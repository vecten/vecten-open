"""Each environment's Snowflake account and the layout it records: the tfvars rules and the
deploy refusal."""

import re

import pytest

from vecten.deploy.snowflake import check_layout, environment_names
from vecten.deploy.tfvars import check_tfvars, env_tfvars_path, environment_accounts
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config, save_config
from vecten_shared.output import ExitCode, Failure

from .scaffold_matrix import DEPLOY, LOCAL


@pytest.fixture
def root(tmp_path):
    answers = {**LOCAL, "sources": ["hubspot"], "warehouse": "snowflake", **DEPLOY}
    answers |= {"cloud": "aws", "snowflake_account": "ACME-PROD"}
    root = tmp_path / "proj"
    render_project(root, ScaffoldParams.from_answers(answers, {}))
    return root


def add_env(root, env, account):
    prod = env_tfvars_path(root, "prod").read_text()
    text = re.sub(r'environment\s*=\s*"prod"', f'environment = "{env}"', prod)
    text = text.replace('"ACME-PROD"', f'"{account}"')
    env_tfvars_path(root, env).write_text(text)


def configure(root, *, shared=None, **facts):
    config = load_config(root)
    if shared is not None:
        config.warehouse.shared_account = shared
    for env, values in facts.items():
        for key, value in values.items():
            setattr(config.environments[env], key, value)
    save_config(root, config)
    return load_config(root)


def refused(call):
    with pytest.raises(Failure) as caught:
        call()
    return caught.value


def test_the_rendered_snowflake_project_passes(root):
    assert check_tfvars(root, load_config(root), "prod")["snowflake_account"] == "ACME-PROD"


def test_each_environment_names_its_own_account(root):
    add_env(root, "dev", "ACME-DEV")
    config = load_config(root)
    assert check_tfvars(root, config, "dev")["snowflake_account"] == "ACME-DEV"
    assert check_tfvars(root, config, "prod")["snowflake_account"] == "ACME-PROD"
    assert environment_accounts(root) == {"dev": "ACME-DEV", "prod": "ACME-PROD"}


def test_prod_alone_must_name_the_account_vecten_toml_names(root):
    env_tfvars_path(root, "prod").write_text(
        env_tfvars_path(root, "prod").read_text().replace('"ACME-PROD"', '"ACME-OTHER"')
    )
    failure = refused(lambda: check_tfvars(root, load_config(root), "prod"))
    assert failure.code == ExitCode.CONFIG
    assert "'ACME-OTHER'" in failure.message and "[warehouse] account" in failure.message


def test_a_dev_environment_needs_no_account_in_vecten_toml(root):
    add_env(root, "dev", "ACME-DEV")
    config = load_config(root)
    config.warehouse.account = None
    save_config(root, config)
    assert check_tfvars(root, load_config(root), "dev")["snowflake_account"] == "ACME-DEV"


@pytest.mark.parametrize("spelled", ["ACME-PROD", "acme-prod"])
def test_two_fixed_environments_in_one_account_are_refused(root, spelled):
    add_env(root, "dev", spelled)
    config = load_config(root)
    for env in ("dev", "prod"):
        failure = refused(lambda env=env: check_tfvars(root, config, env))
        assert failure.code == ExitCode.CONFIG
        assert "deploy/envs/dev.tfvars" in failure.message
        assert "deploy/envs/prod.tfvars" in failure.message
        assert "shared_account" in failure.message


def test_two_environments_in_one_shared_account_pass(root):
    add_env(root, "dev", "ACME-PROD")
    config = configure(root, shared=True)
    for env in ("dev", "prod"):
        assert check_tfvars(root, config, env)["snowflake_account"] == "ACME-PROD"


def test_a_shared_environment_refuses_the_account_of_a_fixed_one(root):
    add_env(root, "dev", "ACME-PROD")
    config = configure(root, shared=True, prod={"snowflake_shared_account": False})
    failure = refused(lambda: check_tfvars(root, config, "dev"))
    assert failure.code == ExitCode.CONFIG
    assert "deploy/envs/prod.tfvars" in failure.message and "fixed" in failure.message


def test_a_shared_environment_beside_a_fixed_prod_in_another_account_passes(root):
    add_env(root, "dev", "ACME-SHARED")
    config = configure(root, shared=True, prod={"snowflake_shared_account": False})
    assert check_tfvars(root, config, "dev")["snowflake_account"] == "ACME-SHARED"
    assert check_tfvars(root, config, "prod")["snowflake_account"] == "ACME-PROD"
    check_layout(root, config, "prod")
    check_layout(root, config, "dev")


def test_a_shared_environment_refuses_a_hyphen_in_its_name(root):
    add_env(root, "dev-2", "ACME-SHARED")
    config = configure(root, shared=True)
    failure = refused(lambda: check_tfvars(root, config, "dev-2"))
    assert "hyphen" in failure.message and "'dev2'" in failure.message


def test_a_fixed_environment_keeps_a_hyphen_in_its_name(root):
    add_env(root, "dev-2", "ACME-DEV")
    assert check_tfvars(root, load_config(root), "dev-2")["environment"] == "dev-2"


def test_a_shared_environment_refuses_a_suffix_over_64_characters(root):
    config = configure(root, shared=True)
    config.warehouse.database = "d" * 60
    save_config(root, config)
    failure = refused(lambda: check_tfvars(root, load_config(root), "prod"))
    assert "64" in failure.message


def test_an_unreadable_tfvars_of_another_environment_is_named(root):
    env_tfvars_path(root, "dev").write_text("not a pair\n")
    failure = refused(lambda: check_tfvars(root, load_config(root), "prod"))
    assert "dev.tfvars" in failure.message


def test_the_layout_record_matching_the_setting_deploys(root):
    config = configure(root, prod={"snowflake_shared_account": False})
    check_layout(root, config, "prod")
    config = configure(root, shared=True, prod={"snowflake_shared_account": True})
    check_layout(root, config, "prod")


def test_an_undeployed_environment_takes_any_setting(root):
    check_layout(root, configure(root, shared=True), "prod")


def test_a_fixed_record_sharing_its_account_refuses_a_shared_setting(root):
    add_env(root, "dev", "ACME-PROD")
    config = configure(root, shared=True, prod={"snowflake_shared_account": False})
    failure = refused(lambda: check_layout(root, config, "prod"))
    assert failure.code == ExitCode.REFUSED
    assert "snowflake_shared_account = false" in failure.message
    assert "deploy/envs/dev.tfvars" in failure.message
    assert "new deployment" in failure.message


def test_an_environment_deployed_without_a_record_reads_as_fixed(root):
    add_env(root, "dev", "ACME-PROD")
    config = configure(root, shared=True, prod={"deployed_commit": "a" * 40})
    failure = refused(lambda: check_layout(root, config, "prod"))
    assert "snowflake_shared_account = false" in failure.message


def test_a_shared_record_refuses_a_fixed_setting(root):
    config = configure(root, prod={"snowflake_shared_account": True})
    failure = refused(lambda: check_layout(root, config, "prod"))
    assert failure.code == ExitCode.REFUSED
    assert "snowflake_shared_account = true" in failure.message
    assert "new deployment" in failure.message


def test_the_cli_takes_the_environments_account_from_its_tfvars(root):
    add_env(root, "dev", "ACME-DEV")
    config = configure(root, shared=True)
    names = environment_names(root, config, "dev")
    assert names.account == "ACME-DEV"
    assert names.analyst_role == "ANALYST_ACME_DEV"
    assert environment_names(root, config, "nosuch").account == "ACME-PROD"
