import copy
import json
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from vecten.deploy import github_env
from vecten.deploy.aws import clients
from vecten.deploy.aws import ship as ship_mod
from vecten.deploy.snowflake import NO_CONFIG_FILE
from vecten.deploy.tfvars import env_tfvars_path
from vecten.github import gh as gh_mod
from vecten.next.deploy import deploy_gate_items
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config, save_config

from .deploy_fixtures import (
    BUCKET,
    COMMIT,
    REGION,
    STANDARD_OUTPUTS,
    STATE_BUCKET,
    FakeGh,
    FakeGit,
    FakeSsm,
    aws_credentials,
    no_aws_credentials,
    render_aws,
    set_facts,
)
from .scaffold_matrix import DEPLOY, LOCAL
from .scaleway_fakes import (
    DEPLOY_SECRET,
    HOST_KEYS,
    STATE_PROJECT,
    FakeTime,
    FakeWatcher,
    actions_key,
    install,
    prepare_environment,
    scaleway_outputs,
)
from .scaleway_fakes import DNS_PROJECT as SCW_DNS_PROJECT
from .scaleway_fakes import artifacts_bucket as scw_artifacts_bucket
from .scaleway_fakes import cloud_ids as scw_cloud_ids
from .scaleway_fakes import state_bucket as scw_state_bucket


def scw_ids() -> dict[str, str]:
    return scw_cloud_ids(["platform", "mcp"])


PLAN_TEXT = "Plan: 42 to add, 0 to change, 0 to destroy.\n"
RUN_URL = "https://github.com/acme/data/actions/runs/42"


class FakeTerraform:
    """Records `(args, cwd, env)` on the class, since the CLI builds its own instance.

    `plan -out=<file>` writes the file, `show` prints `PLAN_TEXT`, `show -json` answers
    `changes` as the plan's `resource_changes`, `output -json` answers the standard outputs;
    `failing` maps a subcommand to the exit code it fails with.
    """

    calls: list = []
    missing = False
    failing: dict = {}
    changes: list = []
    outputs: dict = STANDARD_OUTPUTS
    # A list shared with other fakes, to order Terraform among their calls.
    log: list | None = None

    def which(self):
        return None if self.missing else "/usr/local/bin/terraform"

    def run(self, args, cwd, env=None):
        type(self).calls.append((list(args), Path(cwd), env))
        if self.log is not None:
            self.log.append(("terraform", args[0]))
        command = args[0]
        if command in self.failing:
            return subprocess.CompletedProcess(args, self.failing[command], "", "Error: boom")
        if command == "plan":
            out = next(a.split("=", 1)[1] for a in args if a.startswith("-out="))
            Path(out).write_text("binary plan")
        if command == "show" and "-json" in args:
            body = json.dumps({"format_version": "1.2", "resource_changes": self.changes})
            return subprocess.CompletedProcess(args, 0, body, "")
        if command == "show":
            return subprocess.CompletedProcess(args, 0, PLAN_TEXT, "")
        if command == "output":
            body = {
                k: {"value": v, "type": "x", "sensitive": False} for k, v in self.outputs.items()
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    @classmethod
    def commands(cls) -> list[str]:
        return [args[0] for args, _, _ in cls.calls]

    @classmethod
    def call(cls, command: str) -> list[str]:
        return next(args for args, _, _ in cls.calls if args[0] == command)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "proj")
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:FakeTerraform")
    monkeypatch.setattr(FakeTerraform, "calls", [])
    monkeypatch.setattr(FakeTerraform, "missing", False)
    monkeypatch.setattr(FakeTerraform, "failing", {})
    monkeypatch.setattr(FakeTerraform, "changes", [])
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    return root


@pytest.fixture
def no_cloud(project, monkeypatch):
    no_aws_credentials(monkeypatch, project)

    def refuse(service, region):
        raise AssertionError(f"a {service} client was created")

    monkeypatch.setattr(clients, "client", refuse)
    return project


@pytest.fixture
def bootstrapped(project):
    set_facts(project, bootstrapped=True, state_bucket=STATE_BUCKET)
    return project


@contextmanager
def inside_actions(root: Path, monkeypatch):
    """Inside Actions with the CI role: moto for S3, a fake SSM, health polls that pass."""
    aws_credentials(monkeypatch, root)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    ssm = FakeSsm()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (BUCKET, STATE_BUCKET):
            s3.create_bucket(
                Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION}
            )
        real = clients.client
        monkeypatch.setattr(
            clients, "client", lambda svc, region: ssm if svc == "ssm" else real(svc, region)
        )
        monkeypatch.setattr(ship_mod, "poll", lambda url, timeout, **kw: True)
        yield ssm


@pytest.fixture
def actions(bootstrapped, monkeypatch):
    with inside_actions(bootstrapped, monkeypatch) as ssm:
        yield ssm


@pytest.fixture
def laptop(bootstrapped, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    return gh


def doc(result):
    return json.loads(result.stdout)


def _state_objects() -> list[str]:
    s3 = boto3.client("s3", region_name=REGION)
    return [o["Key"] for o in s3.list_objects_v2(Bucket=STATE_BUCKET).get("Contents", [])]


def _remove_line(root: Path, key: str) -> None:
    path = env_tfvars_path(root, "prod")
    lines = [ln for ln in path.read_text().splitlines() if ln.split("=")[0].strip() != key]
    path.write_text("\n".join(lines) + "\n")


# Inside GitHub Actions


def test_actions_deploy_applies_the_saved_plan_then_ships_every_host(invoke, actions, project):
    before = (project / "vecten.toml").read_text()
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.commands() == ["init", "plan", "show", "apply", "output"]
    root_dir = project / "deploy" / "terraform" / "aws"
    assert {cwd for _, cwd, _ in FakeTerraform.calls} == {root_dir}

    init = FakeTerraform.call("init")
    assert f"-backend-config=bucket={STATE_BUCKET}" in init
    assert "-backend-config=key=prod/aws.tfstate" in init
    assert f"-backend-config=region={REGION}" in init
    assert "-backend-config=use_lockfile=true" in init

    plan = FakeTerraform.call("plan")
    assert "-var-file=../../envs/prod.tfvars" in plan
    out = next(a.split("=", 1)[1] for a in plan if a.startswith("-out="))
    assert FakeTerraform.call("apply")[-1] == out

    written = json.loads((project / ".vecten" / "environments" / "prod.json").read_text())
    assert written == STANDARD_OUTPUTS
    s3 = boto3.client("s3", region_name=REGION)
    body = s3.get_object(Bucket=STATE_BUCKET, Key="prod/outputs.json")["Body"].read()
    assert json.loads(body) == STANDARD_OUTPUTS

    assert actions.kinds() == [("i-0mcp", "ship"), ("i-0platform", "ship")]
    data = doc(r)
    assert data["dispatched"] is False
    assert data["roots"] == ["aws"]
    assert data["hosts"] == ["mcp", "platform"]
    assert data["commit"] == COMMIT
    # CI never writes vecten.toml: the laptop records the deployed commit.
    assert (project / "vecten.toml").read_text() == before
    assert load_config(project).environments["prod"].deployed_commit is None


def test_plan_only_changes_nothing(invoke, actions, project):
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.commands() == ["init", "plan", "show"]
    assert actions.sent == []
    assert _state_objects() == []
    assert not (project / ".vecten" / "environments" / "prod.json").exists()
    data = doc(r)
    plan_file = project / ".vecten" / "plans" / "prod.txt"
    assert Path(data["plan_file"]) == plan_file
    assert PLAN_TEXT in plan_file.read_text()
    assert data["plan_only"] is True
    assert data["roots"] == ["aws"]


def test_postgres_project_applies_no_snowflake_root(invoke, actions, project):
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert all(cwd.name == "aws" for _, cwd, _ in FakeTerraform.calls)
    assert not any("snowflake" in " ".join(args) for args, _, _ in FakeTerraform.calls)


def test_a_failed_apply_ships_nothing_and_uploads_no_outputs(invoke, actions, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "failing", {"apply": 1})
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 5
    error = doc(r)["error"]
    assert "terraform apply" in error and "exit 1" in error
    assert actions.sent == []
    assert _state_objects() == []


def test_actions_without_terraform_is_a_prerequisite(invoke, actions, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "missing", True)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 3
    assert "terraform" in doc(r)["error"]
    assert FakeTerraform.calls == []


# A Snowflake project: the cloud root, then the Snowflake root

DEPLOY_USER_VALUE = "VECTEN_DEPLOY"
DEPLOY_KEY_VALUE = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDdeploykeybody\n"
    "-----END PRIVATE KEY-----\n"
)


def snowflake_outputs() -> dict:
    outputs = copy.deepcopy(STANDARD_OUTPUTS)
    del outputs["postgres"]["warehouse"]
    return outputs


@pytest.fixture
def snowflake(project, tmp_path, monkeypatch):
    root = tmp_path / "snow"
    answers = {**LOCAL, "sources": ["hubspot"], "warehouse": "snowflake", **DEPLOY, "cloud": "aws"}
    render_project(root, ScaffoldParams.from_answers(answers, {"components": ["platform", "mcp"]}))
    path = env_tfvars_path(root, "prod")
    lines = path.read_text().splitlines()
    lines = [
        'snowflake_account = "ACME-PROD"' if ln.startswith("snowflake_account") else ln
        for ln in lines
    ]
    path.write_text("\n".join(lines) + "\n")
    config = load_config(root)
    config.warehouse.account = "ACME-PROD"
    save_config(root, config)
    set_facts(root, bootstrapped=True, state_bucket=STATE_BUCKET)
    monkeypatch.chdir(root)
    monkeypatch.setattr(FakeTerraform, "outputs", snowflake_outputs())
    return root


def _put(name: str, value: str) -> None:
    from vecten_shared.provider import secret_id

    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name=secret_id("acme", "prod", name), SecretString=value
    )


def _stored(name: str) -> str:
    from vecten_shared.provider import secret_id

    sm = boto3.client("secretsmanager", region_name=REGION)
    return sm.get_secret_value(SecretId=secret_id("acme", "prod", name))["SecretString"]


@pytest.fixture
def snowflake_actions(snowflake, monkeypatch):
    with inside_actions(snowflake, monkeypatch) as ssm:
        yield ssm


@pytest.fixture
def deploy_secrets(snowflake_actions):
    _put("DEPLOY_SNOWFLAKE_USER", DEPLOY_USER_VALUE)
    _put("DEPLOY_SNOWFLAKE_PRIVATE_KEY", DEPLOY_KEY_VALUE)
    return snowflake_actions


def _calls_in(root_name: str) -> list:
    return [(args, env) for args, cwd, env in FakeTerraform.calls if cwd.name == root_name]


def _var(args: list[str], name: str) -> str:
    values = [args[i + 1] for i, a in enumerate(args) if a == "-var"]
    return next(v.split("=", 1)[1] for v in values if v.startswith(f"{name}="))


def test_snowflake_project_plans_both_roots_then_applies_cloud_first(
    invoke, deploy_secrets, snowflake
):
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    order = [(args[0], cwd.name) for args, cwd, _ in FakeTerraform.calls]
    assert order == [
        ("init", "aws"),
        ("plan", "aws"),
        ("show", "aws"),
        ("init", "snowflake"),
        ("plan", "snowflake"),
        ("show", "snowflake"),
        ("apply", "aws"),
        ("output", "aws"),
        ("apply", "snowflake"),
    ]
    assert doc(r)["roots"] == ["aws", "snowflake"]
    keys = [
        next(a for a in args if a.startswith("-backend-config=key="))
        for args, _, _ in FakeTerraform.calls
        if args[0] == "init"
    ]
    assert keys == [
        "-backend-config=key=prod/aws.tfstate",
        "-backend-config=key=prod/snowflake.tfstate",
    ]


def test_snowflake_terraform_authenticates_from_the_deploy_secrets(
    invoke, deploy_secrets, snowflake
):
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    for args, env in _calls_in("snowflake"):
        assert env == {
            "SNOWFLAKE_ORGANIZATION_NAME": "ACME",
            "SNOWFLAKE_ACCOUNT_NAME": "PROD",
            "SNOWFLAKE_USER": DEPLOY_USER_VALUE,
            "SNOWFLAKE_AUTHENTICATOR": "SNOWFLAKE_JWT",
            "SNOWFLAKE_PRIVATE_KEY": DEPLOY_KEY_VALUE,
            "SNOWFLAKE_CONFIG_PATH": NO_CONFIG_FILE,
        }, args[0]
    for _, env in _calls_in("aws"):
        assert env is None
    plan = next(args for args, _ in _calls_in("snowflake") if args[0] == "plan")
    assert _var(plan, "database_name") == "ACME"
    assert _var(plan, "warehouse_name") == "ACME_PROD_WH"
    assert _var(plan, "warehouse_size") == "XSMALL"
    assert _var(plan, "loader_public_key").startswith("-----BEGIN PUBLIC KEY-----")
    every_arg = " ".join(a for args, _, _ in FakeTerraform.calls for a in args)
    assert "PRIVATE" not in every_arg
    assert DEPLOY_USER_VALUE not in every_arg
    stored = _stored("SNOWFLAKE_LOADER_PRIVATE_KEY")
    assert stored.startswith("-----BEGIN PRIVATE KEY-----")
    for value in (stored, DEPLOY_KEY_VALUE, DEPLOY_USER_VALUE):
        assert value not in r.stdout + r.stderr


def test_a_second_deploy_reuses_the_stored_loader_key(invoke, deploy_secrets, snowflake):
    assert invoke("deploy", "--env", "prod", "--json").exit_code == 0
    first_key = _stored("SNOWFLAKE_LOADER_PRIVATE_KEY")
    first = _var(next(a for a, _ in _calls_in("snowflake") if a[0] == "plan"), "loader_public_key")
    FakeTerraform.calls.clear()
    assert invoke("deploy", "--env", "prod", "--json").exit_code == 0
    second = _var(next(a for a, _ in _calls_in("snowflake") if a[0] == "plan"), "loader_public_key")
    assert second == first
    assert _stored("SNOWFLAKE_LOADER_PRIVATE_KEY") == first_key


def test_missing_deploy_secrets_refuse_before_any_terraform(invoke, snowflake_actions, snowflake):
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    error = doc(r)["error"]
    assert "vecten secret set DEPLOY_SNOWFLAKE_USER --env prod" in error
    assert "vecten secret set DEPLOY_SNOWFLAKE_PRIVATE_KEY --env prod" in error
    assert FakeTerraform.calls == []
    assert snowflake_actions.sent == []


def test_cloud_only_plans_the_cloud_root_without_reading_deploy_secrets(
    invoke, snowflake_actions, snowflake
):
    r = invoke("deploy", "--env", "prod", "--plan-only", "--cloud-only", "--json")
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.commands() == ["init", "plan", "show"]
    assert {cwd.name for _, cwd, _ in FakeTerraform.calls} == {"aws"}
    assert doc(r)["roots"] == ["aws"]
    sm = boto3.client("secretsmanager", region_name=REGION)
    assert sm.list_secrets()["SecretList"] == []


def test_cloud_only_needs_plan_only(invoke, snowflake_actions, snowflake):
    r = invoke("deploy", "--env", "prod", "--cloud-only", "--json")
    assert r.exit_code == 2
    assert "--plan-only" in doc(r)["error"]
    assert FakeTerraform.calls == []


def test_cloud_only_with_check_is_usage(invoke, snowflake_actions, snowflake):
    r = invoke("deploy", "--env", "prod", "--check", "--cloud-only", "--json")
    assert r.exit_code == 2
    assert "--cloud-only" in doc(r)["error"]


def test_plan_only_on_snowflake_plans_both_roots(invoke, deploy_secrets, snowflake):
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["roots"] == ["aws", "snowflake"]
    text = Path(doc(r)["plan_file"]).read_text()
    assert "deploy/terraform/snowflake" in text
    assert "apply" not in FakeTerraform.commands()


def test_the_first_plan_only_on_snowflake_stores_the_loader_key(invoke, deploy_secrets, snowflake):
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    assert _stored("SNOWFLAKE_LOADER_PRIVATE_KEY").startswith("-----BEGIN PRIVATE KEY-----")
    help_text = " ".join(invoke("deploy", "--help").stdout.split())
    assert "first plan of a Snowflake project stores the loader key" in help_text


def test_a_failed_snowflake_plan_shows_the_account_but_not_the_key(
    invoke, deploy_secrets, snowflake, monkeypatch
):
    key_line = DEPLOY_KEY_VALUE.splitlines()[1]
    stderr = f"Error: account ACME-PROD refused {DEPLOY_USER_VALUE}\n{key_line}\n"

    def run(self, args, cwd, env=None):
        if Path(cwd).name == "snowflake" and args[0] == "plan":
            type(self).calls.append((list(args), Path(cwd), env))
            return subprocess.CompletedProcess(args, 1, "", stderr)
        return original(self, args, cwd, env)

    original = FakeTerraform.run
    monkeypatch.setattr(FakeTerraform, "run", run)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 5
    error = doc(r)["error"]
    assert "account ACME-PROD refused ***" in error
    assert key_line[:16] not in error and DEPLOY_USER_VALUE not in error


def test_provision_returns_the_roots_and_outputs(actions, project):
    from vecten.deploy.aws import AwsProvider

    found = AwsProvider(project, "prod").provision("prod")
    assert found == {"roots": ["aws"], "outputs": STANDARD_OUTPUTS}
    assert FakeTerraform.commands() == ["init", "plan", "show", "apply", "output"]
    assert actions.sent == []
    assert _state_objects() == ["prod/outputs.json"]


def test_only_a_platform_on_snowflake_has_a_snowflake_root(tmp_path):
    from vecten.deploy.snowflake import has_snowflake_root

    from .test_next_project import SNOWFLAKE_URL

    built = tmp_path / "built"
    answers = {**LOCAL, "sources": ["hubspot"], "warehouse": "snowflake", **DEPLOY, "cloud": "aws"}
    render_project(built, ScaffoldParams.from_answers(answers, {}))
    existing = tmp_path / "existing"
    answers = {**LOCAL, "has_warehouse": "yes", "warehouse_url": SNOWFLAKE_URL, **DEPLOY}
    render_project(existing, ScaffoldParams.from_answers({**answers, "cloud": "aws"}, {}))
    assert has_snowflake_root(load_config(built))
    assert (built / "deploy" / "terraform" / "snowflake").is_dir()
    assert not has_snowflake_root(load_config(existing))
    assert not (existing / "deploy" / "terraform" / "snowflake").exists()
    assert not has_snowflake_root(load_config(render_aws(tmp_path / "pg")))


# Refusals before any Terraform


def test_deploy_needs_an_environment(invoke, project):
    r = invoke("deploy", "--json")
    assert r.exit_code == 2
    assert "--env" in doc(r)["error"]


def test_not_bootstrapped_names_bootstrap(invoke, no_cloud, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "vecten bootstrap --env prod" in doc(r)["error"]
    assert FakeTerraform.calls == []


def test_laptop_not_bootstrapped_names_bootstrap(invoke, no_cloud, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "vecten bootstrap --env prod" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_missing_domain_is_named_before_any_terraform(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET)
    _remove_line(no_cloud, "domain")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 6
    assert "domain" in doc(r)["error"]
    assert FakeTerraform.calls == []


def test_unknown_environment_names_its_tfvars(invoke, no_cloud):
    r = invoke("deploy", "--env", "nosuch", "--json")
    assert r.exit_code == 4
    assert "deploy/envs/nosuch.tfvars" in doc(r)["error"]
    assert FakeTerraform.calls == []


def test_local_is_not_a_deploy_environment(invoke, no_cloud):
    r = invoke("deploy", "--env", "local", "--json")
    assert r.exit_code != 0
    assert "local" in doc(r)["error"]
    assert FakeTerraform.calls == []


def test_unsupported_cloud_lists_the_supported_ones(invoke, no_cloud, monkeypatch):
    path = no_cloud / "vecten.toml"
    path.write_text(path.read_text().replace('cloud = "aws"', 'cloud = "gcp"'))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 6
    error = doc(r)["error"]
    assert "'aws'" in error and "'scaleway'" in error
    assert FakeTerraform.calls == []


def test_scaleway_without_a_key_refuses_before_terraform(invoke, tmp_path, monkeypatch, project):
    root = render_aws(tmp_path / "scw", cloud="scaleway")
    monkeypatch.chdir(root)
    set_facts(root, bootstrapped=True, state_bucket=scw_state_bucket(), cloud_ids=scw_ids())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    for name in ("SCW_ACCESS_KEY", "SCW_SECRET_KEY", "SCW_DEFAULT_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SCW_CONFIG_PATH", str(tmp_path / "no-scw-config.yaml"))
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 3
    assert "SCW_ACCESS_KEY" in doc(r)["error"]
    assert FakeTerraform.calls == []


def test_project_without_a_deploy_layer_names_init_deploy(invoke, tmp_path, monkeypatch, project):
    root = tmp_path / "local"
    render_project(root, ScaffoldParams.from_answers({**LOCAL, "sources": ["hubspot"]}, {}))
    monkeypatch.chdir(root)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "vecten init --deploy" in doc(r)["error"]


# From the laptop


def test_laptop_deploy_dispatches_follows_and_records_the_run_commit(
    invoke, laptop, project, monkeypatch
):
    # Local HEAD trails origin/main, so the recorded commit must be the run's, not HEAD.
    git = FakeGit(head="f" * 40)
    monkeypatch.setattr(gh_mod, "get_git", lambda: git)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert git.named("fetch") == [["fetch", "origin", "main"]]
    assert git.named("merge-base") == [["merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"]]
    assert git.named("status") == [["status", "--porcelain", "--", "vecten.toml", "deploy"]]
    assert git.named("rev-list") == [
        ["rev-list", "FETCH_HEAD..HEAD", "--", "vecten.toml", "deploy"]
    ]
    [dispatch] = laptop.named("workflow", "run")
    assert dispatch[:7] == [
        "workflow",
        "run",
        "terraform-apply.yml",
        "--ref",
        "main",
        "-f",
        "environment=prod",
    ]
    assert laptop.named("run", "watch")[0][:4] == ["run", "watch", "42", "--exit-status"]
    assert load_config(project).environments["prod"].deployed_commit == COMMIT
    data = doc(r)
    assert data["dispatched"] is True
    assert data["deployed_commit"] == COMMIT
    assert data["url"] == RUN_URL
    assert FakeTerraform.calls == []


def test_laptop_deploy_records_the_vm_keys_handed_over(invoke, laptop, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    ids = {"project": "p", "retiring_platform": "SCWOLDPLATFORM", "retiring_deploy": "SCWOLDCI"}
    set_facts(project, cloud_ids=ids)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    # The CI key retires on the next bootstrap; the VM's key was handed over by this deploy.
    assert load_config(project).environments["prod"].cloud_ids == {
        "project": "p",
        "retiring_deploy": "SCWOLDCI",
    }


def test_laptop_deploy_clears_the_deploy_gate(invoke, laptop, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    [gate] = deploy_gate_items(load_config(project), "prod")
    assert gate.resolution == "vecten deploy --env prod"
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert deploy_gate_items(load_config(project), "prod") == []


def test_laptop_deploy_off_the_default_branch_names_it(invoke, laptop, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit(answers={("merge-base",): (1, "")}))
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "main" in doc(r)["error"]
    assert laptop.named("workflow", "run") == []


def test_deploy_refuses_unpushed_deploy_files(invoke, laptop, project, monkeypatch):
    git = FakeGit(answers={("status", "--porcelain"): (0, " M vecten.toml\n")})
    monkeypatch.setattr(gh_mod, "get_git", lambda: git)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "vecten github push" in doc(r)["error"]
    assert laptop.named("workflow", "run") == []


def test_deploy_refuses_committed_but_unpushed_deploy_files(invoke, laptop, project, monkeypatch):
    git = FakeGit(answers={("rev-list",): (0, "abc1234\n")})
    monkeypatch.setattr(gh_mod, "get_git", lambda: git)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 4
    error = doc(r)["error"]
    assert "vecten github push" in error and "main" in error
    assert laptop.named("workflow", "run") == []


def test_a_failed_fetch_is_a_tool_failure(invoke, laptop, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit(answers={("fetch",): (128, "")}))
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 5
    assert "git fetch origin main" in doc(r)["error"]
    assert laptop.named("workflow", "run") == []


def test_a_failed_run_exits_5_and_records_nothing(invoke, bootstrapped, monkeypatch):
    gh = FakeGh(watch_code=1)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 5
    assert RUN_URL in doc(r)["error"]
    assert load_config(bootstrapped).environments["prod"].deployed_commit is None


class NoHeadGh(FakeGh):
    """A green run whose view and listing name no head commit."""

    def run(self, args, cwd):
        result = super().run(args, cwd)
        if args[:2] in (["run", "view"], ["run", "list"]):
            body = json.loads(result.stdout)
            for item in body if isinstance(body, list) else [body]:
                item["headSha"] = ""
            result = subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        return result


def test_a_green_run_without_a_head_commit_records_nothing(invoke, bootstrapped, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: NoHeadGh())
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 5
    assert RUN_URL in doc(r)["error"]
    assert load_config(bootstrapped).environments["prod"].deployed_commit is None


def test_laptop_plan_only_plans_locally_and_dispatches_nothing(
    invoke, laptop, project, monkeypatch
):
    aws_credentials(monkeypatch, project)
    with mock_aws():
        r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.commands() == ["init", "plan", "show"]
    assert "-backend-config=key=prod/aws.tfstate" in FakeTerraform.call("init")
    assert laptop.named("workflow", "run") == []
    plan_file = project / ".vecten" / "plans" / "prod.txt"
    assert Path(doc(r)["plan_file"]) == plan_file
    assert PLAN_TEXT in plan_file.read_text()


def test_laptop_plan_only_without_terraform_is_a_prerequisite(invoke, laptop, project, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "missing", True)
    aws_credentials(monkeypatch, project)
    with mock_aws():
        r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 3
    assert "terraform" in doc(r)["error"]
    assert FakeTerraform.calls == []


def test_laptop_plan_only_without_a_login_is_a_prerequisite(invoke, laptop, project, monkeypatch):
    no_aws_credentials(monkeypatch, project)
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 3
    assert "AWS login" in doc(r)["error"]
    assert FakeTerraform.calls == []


# Gates for the workflows


def test_check_reports_facts_without_credentials(invoke, no_cloud, tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    out.write_text("earlier=1\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    r = invoke("deploy", "--env", "prod", "--check", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r) == {"bootstrapped": False, "deployed": False, "ok": True}
    assert out.read_text() == "earlier=1\nbootstrapped=false\ndeployed=false\n"

    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    r = invoke("deploy", "--env", "prod", "--check", "--json")
    assert doc(r) == {"bootstrapped": True, "deployed": True, "ok": True}
    assert out.read_text().endswith("bootstrapped=true\ndeployed=true\n")
    assert FakeTerraform.calls == []


def test_check_needs_no_complete_tfvars(invoke, no_cloud):
    _remove_line(no_cloud, "domain")
    r = invoke("deploy", "--env", "prod", "--check", "--json")
    assert r.exit_code == 0
    assert doc(r) == {"bootstrapped": False, "deployed": False, "ok": True}


# Scaleway, inside GitHub Actions with the deploy key


class ScwPolls:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url, timeout, interval=5, **kwargs):
        self.calls.append(url)
        return True


def _scaleway(root: Path, tmp_path: Path, monkeypatch, env: str = "prod", **output_args):
    from vecten.deploy.scaleway import ship as scw_ship

    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:FakeTerraform")
    monkeypatch.setattr(FakeTerraform, "calls", [])
    monkeypatch.setattr(FakeTerraform, "missing", False)
    monkeypatch.setattr(FakeTerraform, "failing", {})
    monkeypatch.setattr(FakeTerraform, "changes", [])
    org, storage, _ = install(monkeypatch, tmp_path)
    actions_key(monkeypatch)
    outputs = prepare_environment(
        root, org, storage, scaleway_outputs(env, **output_args), env=env, stored=False
    )
    monkeypatch.setattr(FakeTerraform, "outputs", outputs)
    monkeypatch.setattr(FakeTerraform, "log", org.log)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    watcher = FakeWatcher(org, storage, outputs)
    time = FakeTime()
    monkeypatch.setattr(scw_ship, "sleep", time.sleep)
    monkeypatch.setattr(scw_ship, "clock", time.clock)
    polls = ScwPolls()
    monkeypatch.setattr(scw_ship, "poll", polls)
    return type(
        "Scw",
        (),
        {
            "root": root,
            "org": org,
            "storage": storage,
            "outputs": outputs,
            "watcher": watcher,
            "polls": polls,
        },
    )


@pytest.fixture
def scw(tmp_path, monkeypatch):
    return _scaleway(render_aws(tmp_path / "scw", cloud="scaleway"), tmp_path, monkeypatch)


def test_scaleway_deploy_applies_stores_outputs_places_keys_then_ships(invoke, scw):
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    # The second show reads the saved plan's changes, for the database guard.
    assert FakeTerraform.commands() == ["init", "plan", "show", "show", "apply", "output"]
    assert FakeTerraform.calls[3][0][:2] == ["show", "-json"]
    init = FakeTerraform.call("init")
    assert init[-2:] == [
        f"-backend-config=bucket={scw_state_bucket()}",
        "-backend-config=key=prod/scaleway.tfstate",
    ]
    plan = FakeTerraform.call("plan")
    assert "project_id=11111111-0000-4000-8000-000000000001" in plan
    assert any(a.startswith("application_ids={") for a in plan)
    assert f"dns_project_id={SCW_DNS_PROJECT}" in plan
    state = scw.storage.json(scw_state_bucket(), "prod/outputs.json")
    assert state == scw.outputs

    log = [e if len(e) == 4 else (*e, "", "") for e in scw.org.log]
    apply = log.index(("terraform", "apply", "", ""))
    stored = next(
        i for i, e in enumerate(log) if e[1] == "put_object" and e[3] == "prod/outputs.json"
    )
    patches = [i for i, e in enumerate(log) if e[2] == "PATCH"]
    removals = [
        i for i, e in enumerate(log) if e[2] == "DELETE" and "user_data" in e[3] and i > apply
    ]
    first_bundle = next(i for i, e in enumerate(log) if e[1] == "put_object" and "bundles/" in e[3])
    assert apply < stored < min(patches)
    assert max(patches) < min(removals) and max(removals) < first_bundle
    assert all(not s["user_data"] for s in scw.org.servers.values())

    assert [h for h, _ in scw.watcher.pointers] == ["mcp", "platform"]
    data = doc(r)
    assert data["roots"] == ["scaleway"] and data["hosts"] == ["mcp", "platform"]
    assert scw.polls.calls == [
        "https://mcp.data.acme.com/health",
        "https://airflow.data.acme.com/api/v2/monitor/health",
    ]


def _rdb_change(actions: list[str], address: str = 'module.postgres["warehouse"]') -> dict:
    return {
        "address": f"{address}.scaleway_rdb_instance.this",
        "type": "scaleway_rdb_instance",
        "change": {"actions": actions},
    }


@pytest.mark.parametrize(
    "actions", [["delete", "create"], ["create", "delete"], ["delete"]], ids=str
)
def test_scaleway_apply_refuses_to_replace_or_delete_a_database(invoke, scw, actions):
    FakeTerraform.changes = [
        {
            "address": "scaleway_instance_server.host",
            "type": "scaleway_instance_server",
            "change": {"actions": ["update"]},
        },
        _rdb_change(actions),
    ]
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert 'module.postgres["warehouse"].scaleway_rdb_instance.this' in error
    assert "scaleway_instance_server.host" not in error
    assert "snapshot" in error and "docs/runbook.md" in error and "by hand" in error
    assert "apply" not in FakeTerraform.commands()
    assert [h for h, _ in scw.watcher.pointers] == []


def test_scaleway_apply_updates_a_database_in_place(invoke, scw):
    FakeTerraform.changes = [_rdb_change(["update"])]
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert "apply" in FakeTerraform.commands()


def test_aws_apply_reads_no_plan_changes_and_keeps_its_own_protection(invoke, actions, project):
    FakeTerraform.changes = [
        {
            "address": "aws_db_instance.warehouse",
            "type": "aws_db_instance",
            "change": {"actions": ["delete", "create"]},
        }
    ]
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.commands() == ["init", "plan", "show", "apply", "output"]


def test_vm_key_never_reaches_terraform(invoke, scw):
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    placed = scw.org.user_data_written
    assert sorted(placed) == sorted(":".join(HOST_KEYS[h]) for h in ("platform", "mcp"))
    for access, secret in (HOST_KEYS[h] for h in ("platform", "mcp")):
        for args, _, env in FakeTerraform.calls:
            assert secret not in json.dumps(args)
            assert access not in json.dumps(args)
            assert secret not in json.dumps(env or {})
            assert access not in json.dumps(env or {})
        assert secret not in r.stdout
    # Terraform's environment holds the deploy key and nothing else.
    for _, _, env in FakeTerraform.calls:
        assert set(env) == {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "SCW_ACCESS_KEY",
            "SCW_SECRET_KEY",
        }
        assert env["AWS_ACCESS_KEY_ID"].endswith(f"@{STATE_PROJECT}")
        assert env["AWS_SECRET_ACCESS_KEY"] == DEPLOY_SECRET


def test_scaleway_deploy_closes_a_public_endpoint_no_cidrs_ask_for(invoke, scw):
    for instance in scw.org.rdb_instances.values():
        instance["endpoints"].append(
            {"id": f"lb-{instance['id']}", "ip": "51.15.0.9", "port": 20123, "load_balancer": {}}
        )
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    for instance in scw.org.rdb_instances.values():
        assert all(e["load_balancer"] is None for e in instance["endpoints"])
    log = scw.org.log
    closed = [i for i, e in enumerate(log) if e[1] == "rdb" and e[2:3] == ("DELETE",)]
    assert len(closed) == 2 and log.index(("terraform", "apply")) < min(closed)


def test_a_key_a_cancelled_deploy_left_is_deleted_before_terraform_runs(invoke, scw):
    from .scaleway_fakes import SERVERS

    leftover = SERVERS["platform"]
    scw.org.servers[leftover]["user_data"]["vecten-key"] = ":".join(HOST_KEYS["platform"])
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    log = [e if len(e) == 4 else (*e, "", "") for e in scw.org.log]
    first_terraform = next(i for i, e in enumerate(log) if e[0] == "terraform")
    swept = [
        e[3] for e in log[:first_terraform] if e[2] == "DELETE" and e[3].endswith("/vecten-key")
    ]
    # The mcp server holds no entry: its DELETE answers 404, which counts as clean.
    assert sorted(swept) == sorted(
        f"/servers/{SERVERS[h]}/user_data/vecten-key" for h in ("platform", "mcp")
    )
    listing = next(c for c in scw.org.calls if c[:3] == ("instance", "GET", "/servers") and c[4])
    assert listing[4]["project"] == "11111111-0000-4000-8000-000000000001"
    assert listing[4]["tags"] == "vecten:env=prod"
    assert all(not s["user_data"] for s in scw.org.servers.values())


def test_a_full_scaleway_plan_refuses_a_leftover_key_before_terraform_runs(invoke, scw):
    from .scaleway_fakes import SERVERS

    leftover = SERVERS["platform"]
    scw.org.servers[leftover]["user_data"]["vecten-key"] = ":".join(HOST_KEYS["platform"])
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert "vecten-acme-prod-platform" in error and "vecten deploy --env prod" in error
    assert "plan" not in FakeTerraform.commands()
    assert HOST_KEYS["platform"][1] not in r.stdout
    assert scw.org.servers[leftover]["user_data"]
    assert scw.org.writes() == []


def test_a_cloud_only_scaleway_plan_asks_the_servers_nothing(invoke, scw):
    from .scaleway_fakes import SERVERS

    leftover = SERVERS["platform"]
    scw.org.servers[leftover]["user_data"]["vecten-key"] = ":".join(HOST_KEYS["platform"])
    r = invoke("deploy", "--env", "prod", "--plan-only", "--cloud-only", "--json")
    assert r.exit_code == 0, r.stdout
    assert not [c for c in scw.org.calls if c[0] == "instance"]


def test_scaleway_deploy_whose_key_is_never_acknowledged_ships_nothing(invoke, scw):
    scw.watcher.no_ack.add("mcp")
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 5
    assert "mcp" in doc(r)["error"]
    assert scw.watcher.pointers == []
    assert all(not s["user_data"] for s in scw.org.servers.values())


def test_a_second_scaleway_environment_ships_to_its_own_domain(invoke, tmp_path, monkeypatch):
    root = render_aws(tmp_path / "scw", cloud="scaleway")
    prod = env_tfvars_path(root, "prod").read_text()
    staging = prod.replace('"prod"', '"staging"').replace('"data.acme.com"', '"staging.acme.com"')
    env_tfvars_path(root, "staging").write_text(staging)
    env = _scaleway(root, tmp_path, monkeypatch, "staging", domain="staging.acme.com")
    r = invoke("deploy", "--env", "staging", "--json")
    assert r.exit_code == 0, r.stdout
    init = FakeTerraform.call("init")
    assert f"-backend-config=bucket={scw_state_bucket('staging')}" in init
    assert "-backend-config=key=staging/scaleway.tfstate" in init
    assert env.polls.calls == [
        "https://mcp.staging.acme.com/health",
        "https://airflow.staging.acme.com/api/v2/monitor/health",
    ]
    bucket = scw_artifacts_bucket("staging")
    assert env.storage.json(bucket, "bundles/mcp/pointer.json")["commit"] == COMMIT
    assert env.storage.json(scw_state_bucket("staging"), "staging/outputs.json") == env.outputs


# Key expiry


def _expiring_in(project: Path, days: int) -> str:
    moment = datetime.now(UTC) + timedelta(days=days)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    set_facts(project, cloud_ids={"key_deploy": "SCW0123", "expires_deploy": stamp})
    return stamp[:10]


def test_a_key_expiring_within_30_days_is_warned_of_and_deploy_goes_on(invoke, actions, project):
    day = _expiring_in(project, 12)
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    (warning,) = doc(r)["warnings"]
    assert "deploy" in warning and day in warning
    assert "vecten bootstrap --env prod" in warning
    text = invoke("deploy", "--env", "prod", "--plan-only")
    assert text.exit_code == 0
    assert f"warning: {warning}" in text.stdout


def test_a_key_with_more_than_30_days_left_is_not_warned_of(invoke, actions, project):
    _expiring_in(project, 45)
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    assert "warnings" not in doc(r)


def test_the_laptop_dispatch_warns_too(invoke, laptop, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    _expiring_in(project, 2)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["dispatched"] is True
    assert len(doc(r)["warnings"]) == 1


def test_a_pending_snowflake_identity_applies_the_roots_and_ships_nothing(
    invoke, deploy_secrets, snowflake
):
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["roots"] == ["aws", "snowflake"]
    assert data["hosts"] == [] and deploy_secrets.sent == []
    assert "vecten mcp provisioner-script --env prod" in data["ship_skipped"]
    assert "shipped nothing" in invoke("deploy", "--env", "prod").stdout


def test_a_confirmed_snowflake_identity_ships_every_host(invoke, deploy_secrets, snowflake):
    set_facts(snowflake, provisioner_script_run=True)
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["hosts"] == ["mcp", "platform"]
    assert "ship_skipped" not in doc(r)


# Service hostnames: vecten.toml's labels reach the cloud root as one variable.


def _bind_hostnames(root: Path) -> None:
    from .services_fixtures import BOT, with_services

    portal = {
        "command": ["python", "-m", "acme.portal"],
        "port": 8501,
        "health": {"path": "/_stcore/health"},
        "components": ["platform", "mcp"],
        "routes": {"app": {"path": "/", "access": "public"}},
    }
    with_services(
        root,
        {
            "bot": {"hostname": "bot", "routes": {"webhook": {"access": "public"}}},
            "portal": {"hostname": "@", "host": "mcp", "routes": {"app": {"access": "public"}}},
        },
        declared={"bot": BOT, "portal": portal},
    )


def test_plan_only_passes_each_bound_hostname_with_its_host_component(invoke, actions, project):
    _bind_hostnames(project)
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    plan = FakeTerraform.call("plan")
    assert json.loads(_var(plan, "service_hostnames")) == {"bot": "platform", "@": "mcp"}


def test_a_project_without_hostnames_passes_no_service_hostnames(invoke, actions, project):
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    assert not any("service_hostnames" in a for a in FakeTerraform.call("plan"))


def test_a_root_without_the_variable_is_refused_naming_vecten_update(invoke, actions, project):
    _bind_hostnames(project)
    variables = project / "deploy" / "terraform" / "aws" / "variables.tf"
    text = variables.read_text()
    variables.write_text(text[: text.index('variable "service_hostnames"')])
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code != 0
    assert "vecten update" in doc(r)["error"]
    assert FakeTerraform.calls == []


def test_a_root_without_the_variable_still_plans_a_project_without_hostnames(
    invoke, actions, project
):
    variables = project / "deploy" / "terraform" / "aws" / "variables.tf"
    text = variables.read_text()
    variables.write_text(text[: text.index('variable "service_hostnames"')])
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout


def test_a_scaleway_plan_passes_the_bound_hostnames(invoke, scw):
    _bind_hostnames(scw.root)
    r = invoke("deploy", "--env", "prod", "--plan-only", "--json")
    assert r.exit_code == 0, r.stdout
    plan = FakeTerraform.call("plan")
    assert json.loads(_var(plan, "service_hostnames")) == {"bot": "platform", "@": "mcp"}


def test_a_deploy_reports_its_services_after_shipping(invoke, actions, project, monkeypatch):
    from vecten.deploy import shipping

    from .services_fixtures import locked

    _bind_hostnames(project)
    locked(project)
    outputs = copy.deepcopy(STANDARD_OUTPUTS)
    outputs["hosts"]["platform"]["service_hostnames"] = ["bot.data.acme.com"]
    monkeypatch.setattr(FakeTerraform, "outputs", outputs)
    monkeypatch.setattr(shipping, "fetch_response", lambda url: (200, "ok"))
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["services"] == {"acme.bot": "healthy", "acme.portal": "waiting for deploy"}
    text = invoke("deploy", "--env", "prod")
    assert "service acme.bot: healthy" in text.stdout
    assert "routes waiting for `vecten deploy --env prod`: acme.portal.app" in text.stdout


def test_a_destroy_passes_no_hostnames_and_needs_no_lock(project):
    from vecten.deploy.control import service_hostname_vars

    _bind_hostnames(project)
    (project / "extensions.lock.json").unlink()
    root_dir = project / "deploy" / "terraform" / "aws"
    config = load_config(project)
    assert service_hostname_vars(project, config, root_dir, read_only=True) == ()
