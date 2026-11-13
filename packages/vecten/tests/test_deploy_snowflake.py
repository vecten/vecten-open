"""The Snowflake root's inputs: the deploy credentials, the loader key pair and the root itself."""

import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from vecten.deploy.snowflake import (
    DEPLOY_PRIVATE_KEY,
    DEPLOY_ROLE_PRIVILEGES,
    DEPLOY_USER,
    LOADER_PRIVATE_KEY,
    NO_CONFIG_FILE,
    deploy_credentials,
    ensure_loader_key,
    snowflake_root,
    split_account,
)
from vecten.deploy.tfvars import env_tfvars_path
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config, save_config
from vecten_shared.output import ExitCode, Failure

from .deploy_fixtures import render_aws
from .scaffold_matrix import DEPLOY, LOCAL

DEPLOY_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEdeploykeybody0123456789abcdefghijklmnop\n"
    "-----END PRIVATE KEY-----\n"
)


class Store:
    """A secrets store that records reads and writes by name."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.written: list[str] = []
        self.read: list[str] = []

    def get_secret(self, name):
        self.read.append(name)
        if name not in self.values:
            raise Failure(ExitCode.PREREQUISITE, f"secret {name} is not set in prod")
        return self.values[name]

    def put_secret(self, name, value):
        self.written.append(name)
        self.values[name] = value

    def list_secrets(self):
        return sorted(self.values)


def deploy_store(**extra):
    return Store({DEPLOY_USER: "VECTEN_DEPLOY", DEPLOY_PRIVATE_KEY: DEPLOY_KEY, **extra})


def render_snowflake(root, components=("platform", "mcp"), account="ACME-PROD", size=None):
    answers = {**LOCAL, "sources": ["hubspot"], "warehouse": "snowflake", **DEPLOY, "cloud": "aws"}
    render_project(root, ScaffoldParams.from_answers(answers, {"components": list(components)}))
    path = env_tfvars_path(root, "prod")
    lines = []
    for line in path.read_text().splitlines():
        if line.startswith("snowflake_account"):
            line = f'snowflake_account = "{account}"'
        if size and line.startswith("size "):
            line = f'size              = "{size}"'
        lines.append(line)
    path.write_text("\n".join(lines) + "\n")
    config = load_config(root)
    config.warehouse.account = account
    save_config(root, config)
    return root


# The account identifier


@pytest.mark.parametrize(
    ("account", "split"),
    [("ACME-PROD", ("ACME", "PROD")), ("myorg-my_account", ("myorg", "my_account"))],
)
def test_account_splits_into_organisation_and_account(account, split):
    assert split_account(account) == split


@pytest.mark.parametrize("account", ["xy12345", "xy12345.eu-central-1", "-PROD", "ACME-", ""])
def test_an_account_without_its_organisation_is_a_config_error(account):
    with pytest.raises(Failure) as caught:
        split_account(account)
    assert caught.value.code == ExitCode.CONFIG
    assert "ORGANIZATION-ACCOUNT" in caught.value.message


# The deploy credentials


def test_deploy_credentials_configure_the_provider_by_key_pair():
    env = deploy_credentials(deploy_store(), "prod", "ACME-PROD")
    assert env == {
        "SNOWFLAKE_ORGANIZATION_NAME": "ACME",
        "SNOWFLAKE_ACCOUNT_NAME": "PROD",
        "SNOWFLAKE_USER": "VECTEN_DEPLOY",
        "SNOWFLAKE_AUTHENTICATOR": "SNOWFLAKE_JWT",
        "SNOWFLAKE_PRIVATE_KEY": DEPLOY_KEY,
        "SNOWFLAKE_CONFIG_PATH": NO_CONFIG_FILE,
    }
    # No ~/.snowflake/config profile can lay a role or a password over the deploy user.
    assert not Path(NO_CONFIG_FILE).exists()


def test_missing_deploy_credentials_name_both_commands():
    with pytest.raises(Failure) as caught:
        deploy_credentials(Store(), "staging", "ACME-PROD")
    assert caught.value.code == ExitCode.PREREQUISITE
    message = caught.value.message
    assert "vecten secret set DEPLOY_SNOWFLAKE_USER --env staging" in message
    assert "vecten secret set DEPLOY_SNOWFLAKE_PRIVATE_KEY --env staging" in message
    # The first apply sets future grants, which a regular schema allows only with MANAGE GRANTS.
    assert DEPLOY_ROLE_PRIVILEGES in message
    assert "MANAGE GRANTS" in DEPLOY_ROLE_PRIVILEGES


def test_one_missing_credential_is_named_alone():
    store = Store({DEPLOY_USER: "VECTEN_DEPLOY"})
    with pytest.raises(Failure) as caught:
        deploy_credentials(store, "prod", "ACME-PROD")
    assert "DEPLOY_SNOWFLAKE_PRIVATE_KEY --env prod" in caught.value.message
    assert "DEPLOY_SNOWFLAKE_USER --env" not in caught.value.message
    assert "VECTEN_DEPLOY" not in caught.value.message


def test_another_store_failure_is_not_reported_as_missing():
    class Broken(Store):
        def get_secret(self, name):
            raise Failure(ExitCode.TOOL_FAILED, "Secrets Manager GetSecretValue failed: X")

    with pytest.raises(Failure) as caught:
        deploy_credentials(Broken(), "prod", "ACME-PROD")
    assert caught.value.code == ExitCode.TOOL_FAILED


# The loader key pair


def test_a_missing_loader_key_is_generated_and_stored_as_pkcs8():
    store = Store()
    public = ensure_loader_key(store)
    assert store.written == [LOADER_PRIVATE_KEY]
    stored = store.values[LOADER_PRIVATE_KEY]
    assert stored.startswith("-----BEGIN PRIVATE KEY-----\n")
    key = serialization.load_pem_private_key(stored.encode(), password=None)
    assert key.key_size == 2048
    assert public.startswith("-----BEGIN PUBLIC KEY-----\n")
    assert "PRIVATE" not in public
    derived = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert public == derived.decode()


def test_a_stored_loader_key_is_reused():
    store = Store()
    first = ensure_loader_key(store)
    store.written.clear()
    assert ensure_loader_key(store) == first
    assert store.written == []


def test_an_unreadable_loader_key_is_refused_without_its_value():
    store = Store({LOADER_PRIVATE_KEY: "not-a-pem-value-0123456789"})
    with pytest.raises(Failure) as caught:
        ensure_loader_key(store)
    assert caught.value.code == ExitCode.CONFIG
    assert LOADER_PRIVATE_KEY in caught.value.message
    assert "not-a-pem-value" not in caught.value.message
    assert store.written == []


# The root


def test_postgres_and_existing_warehouse_projects_have_no_root(tmp_path):
    from .test_next_project import SNOWFLAKE_URL

    pg = render_aws(tmp_path / "pg")
    store = Store()
    assert snowflake_root(pg, load_config(pg), "prod", store) is None
    existing = tmp_path / "existing"
    answers = {**LOCAL, "has_warehouse": "yes", "warehouse_url": SNOWFLAKE_URL, **DEPLOY}
    render_project(existing, ScaffoldParams.from_answers({**answers, "cloud": "aws"}, {}))
    assert snowflake_root(existing, load_config(existing), "prod", store) is None
    assert store.read == [] and store.written == []


def test_root_carries_state_key_names_size_and_public_key(tmp_path):
    root = render_snowflake(tmp_path / "snow", size="large")
    store = deploy_store()
    found = snowflake_root(root, load_config(root), "prod", store)
    assert found is not None
    assert found.name == "snowflake"
    assert found.root_dir == root / "deploy" / "terraform" / "snowflake"
    assert found.key == "prod/snowflake.tfstate"
    assert found.var_file == "../../envs/prod.tfvars"
    extra = dict(found.extra_vars)
    assert extra["database_name"] == "ACME"
    assert extra["warehouse_name"] == "ACME_PROD_WH"
    # The tier is the environment's own, from its tfvars, not vecten.toml's.
    assert extra["warehouse_size"] == "MEDIUM"
    assert extra["loader_public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert extra["analyst_role_name"] == "ANALYST"
    assert extra["loader_role_name"] == extra["loader_user_name"] == "LOADER"
    assert extra["provisioner_role_name"] == "PROVISIONER"
    assert set(extra) == {
        "database_name",
        "warehouse_name",
        "analyst_role_name",
        "loader_role_name",
        "loader_user_name",
        "provisioner_role_name",
        "warehouse_size",
        "loader_public_key",
    }
    assert all("PRIVATE" not in value for value in extra.values())
    assert found.env["SNOWFLAKE_PRIVATE_KEY"] == DEPLOY_KEY
    assert DEPLOY_KEY not in repr(found)
    # Only what came from the secrets store is masked in Terraform's error output.
    assert set(found.secrets) == {"VECTEN_DEPLOY", DEPLOY_KEY}


def test_missing_credentials_refuse_before_a_loader_key_is_written(tmp_path):
    root = render_snowflake(tmp_path / "snow")
    store = Store()
    with pytest.raises(Failure) as caught:
        snowflake_root(root, load_config(root), "prod", store)
    assert caught.value.code == ExitCode.PREREQUISITE
    assert store.written == []


def test_a_malformed_account_in_tfvars_refuses_before_any_secret_is_read(tmp_path):
    root = render_snowflake(tmp_path / "snow", account="xy12345")
    store = deploy_store()
    with pytest.raises(Failure) as caught:
        snowflake_root(root, load_config(root), "prod", store)
    assert caught.value.code == ExitCode.CONFIG
    assert "deploy/envs/prod.tfvars" in caught.value.message
    assert store.read == []


# The MCP's Snowflake identity


def test_the_identity_is_pending_on_a_platform_snowflake_with_the_mcp(tmp_path):
    from vecten.deploy.snowflake import identity_pending

    root = render_snowflake(tmp_path / "snow")
    config = load_config(root)
    assert identity_pending(config, "prod")
    config.environments["prod"].provisioner_script_run = True
    assert not identity_pending(config, "prod")
    platform_only = load_config(render_snowflake(tmp_path / "plain", components=("platform",)))
    assert not identity_pending(platform_only, "prod")
    assert not identity_pending(load_config(render_aws(tmp_path / "pg")), "prod")


def test_a_provisioner_key_pair_is_kept_under_its_own_name():
    from vecten.deploy.snowflake import PROVISIONER_PRIVATE_KEY, ensure_key_pair

    store = Store()
    public = ensure_key_pair(store, PROVISIONER_PRIVATE_KEY)
    assert store.written == [PROVISIONER_PRIVATE_KEY]
    assert public.startswith("-----BEGIN PUBLIC KEY-----\n")
    assert ensure_key_pair(store, PROVISIONER_PRIVATE_KEY) == public
    assert store.written == [PROVISIONER_PRIVATE_KEY]


def _shared_names(monkeypatch, module):
    from functools import partial

    from vecten_shared.provider import snowflake_names

    monkeypatch.setattr(module, "snowflake_names", partial(snowflake_names, shared_account=True))


def test_the_pending_identity_note_names_the_environments_provisioner(tmp_path, monkeypatch):
    from vecten.deploy import snowflake
    from vecten.deploy.snowflake import identity_pending_note

    config = load_config(render_snowflake(tmp_path / "snow"))
    assert "as VECTEN_PROVISIONER," in identity_pending_note(config, "prod")
    _shared_names(monkeypatch, snowflake)
    note = identity_pending_note(config, "dev")
    assert "as PROVISIONER_ACME_DEV," in note and "VECTEN_PROVISIONER" not in note


def test_the_destroy_message_names_the_environments_provisioner_role():
    from vecten.cli.commands.deploy import _destroy_message

    data = {
        "env": "dev",
        "data_deleted": False,
        "snapshots": [],
        "snapshot_expiry": {},
        "destroyed": [],
        "already_destroyed": [],
        "state_bucket": "b",
        "snowflake": "kept",
        "facts_cleared": ["provisioner_script_run"],
        "outputs_left": [],
        "deployed_commit_cleared": False,
    }
    message = _destroy_message(data, provisioner_role="PROVISIONER_ACME_DEV")
    assert "the users and roles PROVISIONER_ACME_DEV created stay in Snowflake" in message
    with pytest.raises(TypeError):
        _destroy_message(data)


def test_the_root_takes_every_name_from_the_snowflake_names(tmp_path, monkeypatch):
    from vecten.deploy import snowflake

    root = render_snowflake(tmp_path / "snow")
    _shared_names(monkeypatch, snowflake)
    found = snowflake_root(root, load_config(root), "prod", deploy_store())
    extra = dict(found.extra_vars)
    assert (extra["database_name"], extra["warehouse_name"]) == ("ACME_PROD", "ACME_PROD_WH")
    assert extra["analyst_role_name"] == "ANALYST_ACME_PROD"
    assert extra["loader_role_name"] == extra["loader_user_name"] == "LOADER_ACME_PROD"
    assert extra["provisioner_role_name"] == "PROVISIONER_ACME_PROD"
    assert extra["loader_sign_in"] == '{schema="VECTEN_SIGN_IN",policy="SERVICE_SIGN_IN"}'


def test_a_thin_root_without_the_sign_in_variable_refuses_a_shared_account(tmp_path, monkeypatch):
    from vecten.deploy import snowflake

    root = render_snowflake(tmp_path / "snow")
    path = root / "deploy" / "terraform" / "snowflake" / "variables.tf"
    text = path.read_text()
    path.write_text(re.sub(r'variable "loader_sign_in" \{.*?\n\}\n', "", text, flags=re.S))
    assert snowflake_root(root, load_config(root), "prod", deploy_store()) is not None
    _shared_names(monkeypatch, snowflake)
    with pytest.raises(Failure) as caught:
        snowflake_root(root, load_config(root), "prod", deploy_store())
    assert "loader_sign_in" in caught.value.message and "vecten update" in caught.value.message


def _drop_name_variables(root):
    """The thin root a release before shared accounts rendered: no role or user variables."""
    path = root / "deploy" / "terraform" / "snowflake" / "variables.tf"
    kept = [
        line
        for line in path.read_text().splitlines()
        if not any(name in line for name in ("_role_name", "loader_user_name"))
    ]
    path.write_text("\n".join(kept) + "\n")


def test_a_thin_root_without_the_name_variables_keeps_the_fixed_names(tmp_path):
    root = render_snowflake(tmp_path / "snow")
    _drop_name_variables(root)
    found = snowflake_root(root, load_config(root), "prod", deploy_store())
    assert set(dict(found.extra_vars)) == {
        "database_name",
        "warehouse_name",
        "warehouse_size",
        "loader_public_key",
    }


def test_a_thin_root_without_the_name_variables_refuses_other_names(tmp_path, monkeypatch):
    from vecten.deploy import snowflake

    root = render_snowflake(tmp_path / "snow")
    _drop_name_variables(root)
    _shared_names(monkeypatch, snowflake)
    store = deploy_store()
    with pytest.raises(Failure) as caught:
        snowflake_root(root, load_config(root), "prod", store)
    assert caught.value.code == ExitCode.CONFIG
    assert "vecten update" in caught.value.message
    assert "analyst_role_name" in caught.value.message
    assert store.written == []
