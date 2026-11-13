"""`vecten dbt --env <env>`: an environment's own credentials, and nothing built on prod."""

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from vecten import dbt_env
from vecten.cli.commands import dbt as dbt_cmd
from vecten.deploy.tfvars import env_tfvars_path
from vecten_platform.dbt.runner import DbtRun
from vecten_shared import provider as provider_module
from vecten_shared.config import load_config, save_config

from .fixture_project import render_fixture

KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7secretkeybody"
KEY = f"-----BEGIN PRIVATE KEY-----\n{KEY_BODY}\n-----END PRIVATE KEY-----\n"
STORE: dict[str, str] = {}
READ: list[str] = []


class FakeCloud:
    def __init__(self, project_root, env):
        self.env = env

    def get_secret(self, name):
        READ.append(f"{self.env}/{name}")
        return STORE[name]

    def put_secret(self, name, value):
        raise AssertionError("vecten dbt never writes a secret")

    def list_secrets(self):
        return sorted(STORE)

    def connection(self, env, db):
        raise AssertionError("vecten dbt --env builds its connection from vecten.toml")


def _manifest(*nodes):
    return {"nodes": {n["unique_id"]: n for n in nodes}}


HOOK = {
    "unique_id": "operation.acme.acme-on-run-start-0",
    "resource_type": "operation",
    "name": "acme-on-run-start-0",
    "package_name": "acme",
    "config": {},
}
PLAIN_TEST = {
    "unique_id": "test.acme.not_null_company_company_id.1a",
    "resource_type": "test",
    "name": "not_null_company_company_id",
    "package_name": "acme",
    "config": {"store_failures": None, "store_failures_as": None},
}


def _storing(**config):
    return {
        **PLAIN_TEST,
        "unique_id": "test.acme.unique_company_company_id.2b",
        "name": "unique_company_company_id",
        "config": {"store_failures": None, "store_failures_as": None, **config},
    }


@pytest.fixture
def snowflake(tmp_path, monkeypatch):
    """A deployed Snowflake project whose dbt runs and `dbt parse` are recorded, not run."""
    STORE.clear()
    READ.clear()
    STORE["SNOWFLAKE_LOADER_PRIVATE_KEY"] = KEY
    root = render_fixture(tmp_path, "snowflake", deploy=True)
    monkeypatch.chdir(root)
    monkeypatch.setitem(provider_module._REGISTRY, "aws", FakeCloud)
    runs = []
    parses = []
    manifest = {"doc": _manifest(PLAIN_TEST)}

    def fake_run(args, *, project_dir, env, capture=True):
        runs.append({"args": args, "env": env, "capture": capture})
        return DbtRun(3, "", "")

    def fake_parse(args, *, project_dir, env, capture=True):
        parses.append({"args": args, "env": env})
        target = Path(env["DBT_TARGET_PATH"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(json.dumps(manifest["doc"]))
        return DbtRun(0, "", "")

    monkeypatch.setattr(dbt_cmd, "run_dbt", fake_run)
    monkeypatch.setattr(dbt_env, "run_dbt", fake_parse)
    return {"root": root, "runs": runs, "parses": parses, "manifest": manifest}


ALLOWED_ON_PROD = [
    ["compile"],
    ["compile", "--select", "marts"],
    ["test"],
    ["test", "--select", "company"],
    ["ls"],
    ["list", "--resource-type", "model"],
    ["show", "--select", "sales_pipeline"],
    ["show", "--inline", "select 1"],
    ["debug"],
    ["parse"],
    ["source", "freshness"],
    ["docs", "generate"],
    ["docs", "serve", "--port", "8081"],
    ["deps"],
    ["clean"],
    ["--debug", "test"],
    ["--log-level", "debug", "compile"],
]
REFUSED_ON_PROD = [
    ["run"],
    ["run", "--select", "marts"],
    ["build"],
    ["seed"],
    ["snapshot"],
    ["clone", "--state", "x"],
    ["retry"],
    ["run-operation", "grant_select"],
    ["init"],
    ["--debug", "run"],
    ["docs"],
    ["source"],
    ["unknown-command"],
    [],
    ["--help"],
    ["--vars", "{a: 1}", "test"],
]


@pytest.mark.parametrize("args", ALLOWED_ON_PROD, ids=" ".join)
def test_prod_runs_commands_that_build_nothing(invoke, snowflake, args):
    result = invoke("dbt", "--env", "prod", *args)
    assert result.exit_code == 3, result.stderr
    assert [r["args"] for r in snowflake["runs"]] == [args]
    assert snowflake["runs"][0]["capture"] is False
    env = snowflake["runs"][0]["env"]
    assert env["SNOWFLAKE_PRIVATE_KEY"] == KEY
    assert env["SNOWFLAKE_USER"] == env["SNOWFLAKE_ROLE"] == "LOADER"
    assert env["SNOWFLAKE_ACCOUNT"] == "ACME-PROD"
    assert env["SNOWFLAKE_DATABASE"] == "ACME"
    assert env["SNOWFLAKE_WAREHOUSE"] == "ACME_PROD_WH"
    assert env["DBT_PROFILES_DIR"] == str(snowflake["root"] / "dbt")
    assert READ == ["prod/SNOWFLAKE_LOADER_PRIVATE_KEY"]


@pytest.mark.parametrize("args", REFUSED_ON_PROD, ids=lambda a: " ".join(a) or "nothing")
def test_prod_refuses_every_other_command(invoke, snowflake, args):
    result = invoke("dbt", "--env", "prod", *args)
    assert result.exit_code == 4
    assert snowflake["runs"] == [] and snowflake["parses"] == []
    assert READ == []
    assert "build nothing" in result.stderr


def test_the_refusal_names_the_command_and_what_prod_allows(invoke, snowflake):
    result = invoke("dbt", "--env", "prod", "run-operation", "grant_select")
    assert "`dbt run-operation`" in result.stderr
    assert "compile" in result.stderr and "source freshness" in result.stderr


@pytest.mark.parametrize(
    "args", [["run"], ["build", "--full-refresh"], ["seed"], ["run-operation", "grant_select"]]
)
def test_other_environments_run_every_command(invoke, snowflake, args):
    result = invoke("dbt", "--env", "staging", *args)
    assert result.exit_code == 3, result.stderr
    assert [r["args"] for r in snowflake["runs"]] == [args]
    env = snowflake["runs"][0]["env"]
    assert env["SNOWFLAKE_WAREHOUSE"] == "ACME_STAGING_WH"
    assert env["SNOWFLAKE_PRIVATE_KEY"] == KEY
    assert READ == ["staging/SNOWFLAKE_LOADER_PRIVATE_KEY"]
    assert snowflake["parses"] == []


def test_a_shared_environment_connects_with_its_own_names_and_account(invoke, snowflake):
    root = snowflake["root"]
    prod = env_tfvars_path(root, "prod").read_text()
    staging = re.sub(r'environment\s*=\s*"prod"', 'environment = "staging"', prod)
    staging = re.sub(
        r'snowflake_account\s*=\s*"[^"]*"', 'snowflake_account = "ACME-SHARED"', staging
    )
    env_tfvars_path(root, "staging").write_text(staging)
    config = load_config(root)
    config.warehouse.shared_account = True
    save_config(root, config)
    result = invoke("dbt", "--env", "staging", "run")
    assert result.exit_code == 3, result.stderr
    env = snowflake["runs"][0]["env"]
    assert env["SNOWFLAKE_USER"] == env["SNOWFLAKE_ROLE"] == "LOADER_ACME_STAGING"
    assert (env["SNOWFLAKE_DATABASE"], env["SNOWFLAKE_WAREHOUSE"]) == (
        "ACME_STAGING",
        "ACME_STAGING_WH",
    )
    assert env["SNOWFLAKE_ACCOUNT"] == "ACME-SHARED"


def test_other_environments_run_hooks_and_store_failures(invoke, snowflake):
    snowflake["manifest"]["doc"] = _manifest(HOOK, _storing(store_failures=True))
    result = invoke("dbt", "--env", "staging", "test", "--store-failures")
    assert result.exit_code == 3, result.stderr
    assert [r["args"] for r in snowflake["runs"]] == [["test", "--store-failures"]]
    assert "DBT_EXCLUDE_RESOURCE_TYPES" not in snowflake["runs"][0]["env"]


@pytest.mark.parametrize("flag", ["--store-failures", "--store-failures=true"])
def test_prod_refuses_test_with_the_store_failures_flag(invoke, snowflake, flag):
    result = invoke("dbt", "--env", "prod", "test", flag)
    assert result.exit_code == 4
    assert snowflake["runs"] == [] and READ == []
    # dbt's parser takes no value for the flag, so the second spelling names no command it reads.
    assert "--store-failures" in result.stderr or "no dbt command" in result.stderr


@pytest.mark.parametrize(
    "config",
    [{"store_failures": True}, {"store_failures_as": "table"}, {"store_failures_as": "view"}],
)
def test_prod_refuses_test_when_a_test_stores_its_failures(invoke, snowflake, config):
    snowflake["manifest"]["doc"] = _manifest(PLAIN_TEST, _storing(**config))
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 4
    assert snowflake["runs"] == [] and READ == []
    assert "unique_company_company_id" in result.stderr
    assert "not_null_company_company_id" not in result.stderr


def test_a_test_that_stores_failures_as_ephemeral_writes_nothing(invoke, snowflake):
    snowflake["manifest"]["doc"] = _manifest(_storing(store_failures_as="ephemeral"))
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 3, result.stderr


@pytest.mark.parametrize("args", [["test"], ["source", "freshness"]])
def test_prod_refuses_commands_that_execute_on_run_hooks(invoke, snowflake, args):
    snowflake["manifest"]["doc"] = _manifest(PLAIN_TEST, HOOK)
    result = invoke("dbt", "--env", "prod", *args)
    assert result.exit_code == 4
    assert snowflake["runs"] == [] and READ == []
    assert "acme-on-run-start-0" in result.stderr and "on-run" in result.stderr


@pytest.mark.parametrize("args", [["compile"], ["show", "--select", "x"], ["docs", "generate"]])
def test_commands_that_never_execute_hooks_run_beside_them(invoke, snowflake, args):
    snowflake["manifest"]["doc"] = _manifest(HOOK, _storing(store_failures=True))
    result = invoke("dbt", "--env", "prod", *args)
    assert result.exit_code == 3, result.stderr
    assert snowflake["parses"] == []


def test_the_check_parses_with_the_projects_vars_and_without_the_credential(invoke, snowflake):
    result = invoke("dbt", "--env", "prod", "test", "--vars", "{audit: true}")
    assert result.exit_code == 3, result.stderr
    (parse,) = snowflake["parses"]
    assert parse["args"] == ["parse", "--vars", "{audit: true}"]
    assert KEY_BODY not in json.dumps(parse["env"])
    assert parse["env"]["DBT_TARGET_PATH"] != str(snowflake["root"] / "dbt" / "target")
    assert not Path(parse["env"]["DBT_TARGET_PATH"]).exists()


def test_a_failed_check_parse_stops_the_run(invoke, snowflake, monkeypatch):
    def failing_parse(args, *, project_dir, env, capture=True):
        return DbtRun(2, "", "Compilation Error: dbt found 1 package(s) but only 0 installed")

    monkeypatch.setattr(dbt_env, "run_dbt", failing_parse)
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 5
    assert snowflake["runs"] == [] and READ == []
    assert "dbt parse" in result.stderr and "only 0 installed" in result.stderr


SHELL_DBT_SETTINGS = {
    "DBT_STORE_FAILURES": "true",
    "DBT_ENGINE_STORE_FAILURES": "true",
    "DBT_ENGINE_EXCLUDE_RESOURCE_TYPES": "model",
    "DBT_ENGINE_TARGET": "audit",
    "DBT_ENGINE_PROFILES_DIR": "/elsewhere",
    "DBT_ENGINE_PROJECT_DIR": "/elsewhere",
    "DBT_PROFILE": "other",
}


def _effective(args, env):
    """The options dbt's own parser resolves for `args` under the process environment `env`."""
    import click
    from dbt.cli.main import cli

    command = cli.commands[args[0]]
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        ctx = command.make_context(args[0], list(args[1:]), parent=click.Context(cli))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return ctx.params


def test_prod_runs_without_the_shells_dbt_settings(invoke, snowflake, monkeypatch):
    for name, value in SHELL_DBT_SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DBT_ENV_CUSTOM_ENV_TEAM", "data")
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 3, result.stderr
    (parse,) = snowflake["parses"]
    run = snowflake["runs"][0]["env"]
    for env in (parse["env"], run):
        assert not set(SHELL_DBT_SETTINGS) & set(env)
        assert env["DBT_ENV_CUSTOM_ENV_TEAM"] == "data"
    assert run["DBT_EXCLUDE_RESOURCE_TYPES"] == "unit_test"
    effective = _effective(["test"], run)
    assert effective["exclude_resource_types"] == ("unit_test",)
    assert effective["store_failures"] is False
    assert effective["target"] == "local"
    assert effective["project_dir"] == effective["profiles_dir"] == str(snowflake["root"] / "dbt")


def test_other_environments_keep_the_shells_dbt_settings(invoke, snowflake, monkeypatch):
    monkeypatch.setenv("DBT_ENGINE_STORE_FAILURES", "true")
    result = invoke("dbt", "--env", "staging", "test")
    assert result.exit_code == 3, result.stderr
    assert snowflake["runs"][0]["env"]["DBT_ENGINE_STORE_FAILURES"] == "true"


@pytest.mark.parametrize(
    "flag",
    [
        ["--exclude-resource-type", "test"],
        ["--exclude-resource-type=test"],
        ["--exclude-resource-types", "model"],
        ["--exclude-resource-types=model"],
    ],
)
def test_prod_refuses_test_that_would_bring_unit_tests_back(invoke, snowflake, flag):
    result = invoke("dbt", "--env", "prod", "test", *flag)
    assert result.exit_code == 4
    assert snowflake["runs"] == [] and READ == []
    assert "unit test" in result.stderr


@pytest.mark.parametrize("command", [["test"], ["compile"], ["source", "freshness"]])
@pytest.mark.parametrize(
    "option",
    [
        ["--target", "audit"],
        ["--target=audit"],
        ["-t", "audit"],
        ["-taudit"],
        ["--profile", "other"],
        ["--profiles-dir", "."],
        ["--project-dir", "."],
    ],
    ids=" ".join,
)
def test_prod_refuses_another_project_profile_or_target(invoke, snowflake, command, option):
    result = invoke("dbt", "--env", "prod", *command, *option)
    assert result.exit_code == 4, result.stderr
    assert snowflake["runs"] == [] and snowflake["parses"] == [] and READ == []
    assert "chooses the dbt project, profile and target" in result.stderr


def test_another_target_is_allowed_on_other_environments(invoke, snowflake):
    result = invoke("dbt", "--env", "staging", "test", "--target", "audit")
    assert result.exit_code == 3, result.stderr


def test_the_credential_reaches_dbt_only_through_its_environment(invoke, snowflake):
    root = snowflake["root"]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = invoke("dbt", "--env", "prod", "debug")
    assert result.exit_code == 3, result.stderr
    env = snowflake["runs"][0]["env"]
    assert env["SNOWFLAKE_PRIVATE_KEY"] == KEY
    # dbt replaces the value of every DBT_ENV_SECRET_* variable in what it logs and prints.
    assert KEY in [v for k, v in env.items() if k.startswith("DBT_ENV_SECRET_")]
    assert KEY_BODY not in result.stdout + result.stderr
    for path in root.rglob("*"):
        if path.is_file():
            assert KEY_BODY.encode() not in path.read_bytes(), path
    assert (root / "dbt" / "profiles.yml").read_bytes() == before[root / "dbt" / "profiles.yml"]
    assert KEY_BODY not in os.environ.get("SNOWFLAKE_PRIVATE_KEY", "")


def test_env_on_a_postgres_project_is_refused(invoke, tmp_path, monkeypatch):
    READ.clear()
    root = render_fixture(tmp_path, "postgres", deploy=True)
    monkeypatch.chdir(root)
    monkeypatch.setitem(provider_module._REGISTRY, "aws", FakeCloud)
    calls = []
    monkeypatch.setattr(dbt_cmd, "run_dbt", lambda *a, **k: calls.append(a))
    result = invoke("dbt", "--env", "prod", "compile")
    assert result.exit_code == 4
    assert calls == [] and READ == []
    assert "private network" in result.stderr and "Snowflake" in result.stderr


def test_env_needs_a_deployed_project(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    result = invoke("dbt", "--env", "prod", "compile")
    assert result.exit_code == 4
    assert "vecten init --deploy" in result.stderr


def test_env_needs_the_snowflake_account(invoke, snowflake):
    root = snowflake["root"]
    toml = root / "vecten.toml"
    toml.write_text(toml.read_text().replace('account = "ACME-PROD"\n', ""))
    # The environment's tfvars name its account, which is enough on its own.
    assert invoke("dbt", "--env", "prod", "compile").exit_code == 3
    tfvars = env_tfvars_path(root, "prod")
    tfvars.write_text(re.sub(r"snowflake_account\s*=.*\n", "", tfvars.read_text()))
    READ.clear()
    result = invoke("dbt", "--env", "prod", "compile")
    assert result.exit_code == 6
    assert "account" in result.stderr and READ == []


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["--env"], 2),
        (["--env="], 2),
        (["--env", "local", "compile"], 2),
        (["--env", "Prod!", "compile"], 2),
        (["compile", "--env", "prod"], 2),
    ],
)
def test_env_is_one_cloud_environment_given_first(invoke, snowflake, args, code):
    result = invoke("dbt", *args)
    assert result.exit_code == code
    assert snowflake["runs"] == [] and READ == []


def test_env_with_an_equals_sign(invoke, snowflake):
    result = invoke("dbt", "--env=prod", "compile")
    assert result.exit_code == 3, result.stderr
    assert [r["args"] for r in snowflake["runs"]] == [["compile"]]


def test_the_missing_loader_key_names_the_deploy(invoke, snowflake):
    STORE.clear()

    def missing(self, name):
        from vecten_shared.output import ExitCode, Failure

        raise Failure(ExitCode.PREREQUISITE, f"secret {name} is not set in {self.env}")

    FakeCloud.get_secret, saved = missing, FakeCloud.get_secret
    try:
        result = invoke("dbt", "--env", "prod", "compile")
    finally:
        FakeCloud.get_secret = saved
    assert result.exit_code == 3
    assert "SNOWFLAKE_LOADER_PRIVATE_KEY" in result.stderr
    assert "vecten deploy --env prod" in result.stderr


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (["test"], ("test",)),
        (["--debug", "test", "-s", "x"], ("test",)),
        (["--log-level", "debug", "run"], ("run",)),
        (["docs", "generate"], ("docs", "generate")),
        (["source", "freshness", "--select", "x"], ("source", "freshness")),
        (["ls"], ("list",)),
        (["test", "--", "run"], ("test",)),
        (["test", "--no-such-option"], ()),
        (["nonsense"], ()),
        ([], ()),
        (["--help"], ()),
    ],
)
def test_dbt_command_is_read_with_dbts_own_parser(args, command):
    assert dbt_env.dbt_command(args) == command


def _real_parse(snowflake, monkeypatch) -> Path:
    """The project's dbt folder, with its packages installed and the prod check parsing for real."""
    from vecten_platform.dbt import runner
    from vecten_shared.config import load_config

    root = snowflake["root"]
    monkeypatch.setattr(dbt_env, "run_dbt", runner.run_dbt)
    dbt_dir = root / "dbt"
    runner.resolve_dependencies(
        project_dir=dbt_dir,
        config=load_config(root),
        env=runner.base_env(dbt_dir, {}),
        in_process=False,
    )
    return dbt_dir


def test_the_prod_check_reads_hooks_and_stored_failures_from_a_real_parse(
    invoke, snowflake, monkeypatch
):
    """dbt itself parses the project, so a hook or store_failures set anywhere dbt reads is seen."""
    dbt_dir = _real_parse(snowflake, monkeypatch)
    assert invoke("dbt", "--env", "prod", "test").exit_code == 3

    project = yaml.safe_load((dbt_dir / "dbt_project.yml").read_text())
    with_hook = {**project, "on-run-end": ["grant select on all tables in schema marts to role x"]}
    (dbt_dir / "dbt_project.yml").write_text(yaml.safe_dump(with_hook, sort_keys=False))
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 4 and "on-run-end" in result.stderr, result.stderr

    storing = {**project, "data_tests": {"+store_failures": True}}
    (dbt_dir / "dbt_project.yml").write_text(yaml.safe_dump(storing, sort_keys=False))
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 4 and "store" in result.stderr, result.stderr
    assert len(snowflake["runs"]) == 1 and READ == ["prod/SNOWFLAKE_LOADER_PRIVATE_KEY"]


def test_the_prod_run_uses_the_target_its_check_parsed(invoke, snowflake, monkeypatch):
    """A test config that depends on the target is checked against the target the run uses."""
    from vecten_platform.dbt import runner
    from vecten_shared.output import Failure

    dbt_dir = _real_parse(snowflake, monkeypatch)
    profiles = yaml.safe_load((dbt_dir / "profiles.yml").read_text())
    outputs = profiles["vecten"]["outputs"]
    outputs["audit"] = dict(outputs["local"])
    (dbt_dir / "profiles.yml").write_text(yaml.safe_dump(profiles, sort_keys=False))
    project = yaml.safe_load((dbt_dir / "dbt_project.yml").read_text())
    by_target = {**project, "data_tests": {"+store_failures": "{{ target.name == 'audit' }}"}}
    (dbt_dir / "dbt_project.yml").write_text(yaml.safe_dump(by_target, sort_keys=False))
    with pytest.raises(Failure, match="store"):
        audit = {"DBT_TARGET": "audit", "SNOWFLAKE_DATABASE": "ACME"}
        dbt_env.check_prod_project(dbt_dir, ("test",), [], runner.base_env(dbt_dir, audit))

    result = invoke("dbt", "--env", "prod", "test", "--target", "audit")
    assert result.exit_code == 4 and "target" in result.stderr, result.stderr

    monkeypatch.setenv("DBT_ENGINE_TARGET", "audit")
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 3, result.stderr
    (run,) = snowflake["runs"]
    assert run["env"]["DBT_TARGET"] == "local" and "DBT_ENGINE_TARGET" not in run["env"]


def test_prod_passes_the_declared_areas_to_the_run_and_its_check(invoke, snowflake):
    from vecten_shared.config import AreaDecl, load_config, save_config

    root = snowflake["root"]
    save_config(root, load_config(root).model_copy(update={"areas": {"finance": AreaDecl()}}))
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 3, result.stderr
    areas = ["--vars", '{"vecten_areas": ["finance"]}']
    assert snowflake["runs"][0]["args"] == ["test", *areas]
    assert snowflake["parses"][0]["args"] == ["parse", *areas]


CONVENTION_HOOK = {
    "unique_id": "operation.vecten_convention.vecten_convention-on-run-start-0",
    "resource_type": "operation",
    "name": "vecten_convention-on-run-start-0",
    "package_name": "vecten_convention",
    "config": {},
}


def test_prod_runs_beside_the_convention_packages_grants_check(invoke, snowflake):
    snowflake["manifest"]["doc"] = _manifest(CONVENTION_HOOK, PLAIN_TEST)
    result = invoke("dbt", "--env", "prod", "test")
    assert result.exit_code == 3, result.stderr
    assert len(snowflake["runs"]) == 1
