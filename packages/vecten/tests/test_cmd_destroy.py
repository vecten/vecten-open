import json
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from vecten.cli.commands import deploy as deploy_cmd
from vecten.deploy.names import rds_identifier
from vecten.deploy.run import destroy_env
from vecten.deploy.tfvars import env_tfvars_path
from vecten.password_links import added, read_links
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config, save_config
from vecten_shared.provider import secret_id
from vecten_shared.records import read_record, write_record

from .deploy_fixtures import (
    COMMIT,
    REGION,
    STANDARD_OUTPUTS,
    STATE_BUCKET,
    aws_credentials,
    minimal_outputs,
    render_aws,
    set_facts,
)
from .scaffold_matrix import DEPLOY, LOCAL
from .scaleway_fakes import FakeTime, install, prepare_environment, scaleway_outputs
from .scaleway_fakes import state_bucket as scw_state_bucket

DEPLOY_USER_VALUE = "VECTEN_DEPLOY_USER"
DEPLOY_KEY_VALUE = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDdeploykeybody\n"
    "-----END PRIVATE KEY-----\n"
)


STATE_INSTANCE = 'module.environment.module.postgres["state"].aws_db_instance.this'
WAREHOUSE_INSTANCE = 'module.environment.module.postgres["warehouse"].aws_db_instance.this'
STATE_PARAMS = 'module.environment.module.postgres["state"].aws_db_parameter_group.this'
BUCKET_ADDRESS = "module.environment.aws_s3_bucket.artifacts"
VPC = "module.environment.aws_vpc.this"
UPDATE_ONLY = [
    {"address": STATE_INSTANCE, "change": {"actions": ["update"]}},
    {"address": WAREHOUSE_INSTANCE, "change": {"actions": ["update"]}},
    {
        "address": "module.environment.aws_s3_bucket.artifacts",
        "change": {"actions": ["update"]},
    },
    {"address": "module.environment.aws_vpc.this", "change": {"actions": ["no-op"]}},
]
STATE = {
    "aws": [VPC, STATE_INSTANCE, STATE_PARAMS, WAREHOUSE_INSTANCE, BUCKET_ADDRESS],
    "snowflake": ["snowflake_database.this"],
}


class FakeTerraform:
    """Records each call as `("terraform", args, root name)` in the shared `events` list.

    `state list` answers from `state[root]`. `show -json` answers from `changes` as Terraform
    would plan them: only targeted addresses (or a change whose `needed_by` is targeted, a
    dependency the plan pulls in), and no update of a resource absent from state.
    """

    events: list = []
    missing = False
    failing: dict = {}
    state: dict = STATE
    changes: list = UPDATE_ONLY
    targets: list = []

    def which(self):
        return None if self.missing else "/usr/local/bin/terraform"

    def run(self, args, cwd, env=None):
        type(self).events.append(("terraform", list(args), Path(cwd).name))
        if args[0] in self.failing:
            return subprocess.CompletedProcess(args, self.failing[args[0]], "", "Error: boom")
        if args[0] == "plan":
            out = next(a.split("=", 1)[1] for a in args if a.startswith("-out="))
            Path(out).write_text("binary plan")
            type(self).targets = [a.split("=", 1)[1] for a in args if a.startswith("-target=")]
        if args[:2] == ["state", "list"]:
            lines = "".join(f"{a}\n" for a in self.state.get(Path(cwd).name, []))
            return subprocess.CompletedProcess(args, 0, lines, "")
        if args[0] == "show" and "-json" in args:
            in_state = set(self.state.get(Path(cwd).name, []))
            planned = [
                c
                for c in self.changes
                if (c["address"] in self.targets or c.get("needed_by") in self.targets)
                and ("create" in c["change"]["actions"] or c["address"] in in_state)
            ]
            body = json.dumps({"format_version": "1.2", "resource_changes": planned})
            return subprocess.CompletedProcess(args, 0, body, "")
        if args[0] == "show":
            return subprocess.CompletedProcess(args, 0, "Plan: 0 to add, 3 to change.\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    @classmethod
    def calls(cls) -> list[tuple[str, str]]:
        return [(e[1][0], e[2]) for e in cls.events if e[0] == "terraform"]

    @classmethod
    def call(cls, command: str, root: str = "aws") -> list[str]:
        return next(
            e[1] for e in cls.events if e[0] == "terraform" and e[1][0] == command and e[2] == root
        )


def _vars(args: list[str]) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == "-var"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "proj")
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:FakeTerraform")
    monkeypatch.setattr(FakeTerraform, "events", [])
    monkeypatch.setattr(FakeTerraform, "missing", False)
    monkeypatch.setattr(FakeTerraform, "failing", {})
    monkeypatch.setattr(FakeTerraform, "targets", [])
    monkeypatch.setattr(FakeTerraform, "state", STATE)
    monkeypatch.setattr(FakeTerraform, "changes", UPDATE_ONLY)
    set_facts(root, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    return root


def _add_env(root: Path, env: str) -> None:
    prod = env_tfvars_path(root, "prod").read_text()
    env_tfvars_path(root, env).write_text(
        prod.replace('environment       = "prod"', f'environment       = "{env}"')
    )
    set_facts(root, env, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)


@contextmanager
def cloud(root: Path, monkeypatch, outputs: dict | None = STANDARD_OUTPUTS, env: str = "prod"):
    """Moto with the state bucket, the environment's stored outputs and its RDS instances."""
    aws_credentials(monkeypatch, root)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=STATE_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        if outputs is not None:
            stored = {**outputs, "environment": env}
            s3.put_object(Bucket=STATE_BUCKET, Key=f"{env}/outputs.json", Body=json.dumps(stored))
            rds = boto3.client("rds", region_name=REGION)
            for key in outputs["postgres"]:
                rds.create_db_instance(
                    DBInstanceIdentifier=rds_identifier("acme", env, key),
                    Engine="postgres",
                    DBInstanceClass="db.t4g.micro",
                    AllocatedStorage=20,
                    MasterUsername="vecten",
                    MasterUserPassword="not-a-real-password",
                )
        yield


def _snapshots() -> list[str]:
    rds = boto3.client("rds", region_name=REGION)
    return sorted(
        s["DBSnapshotIdentifier"]
        for s in rds.describe_db_snapshots(SnapshotType="manual")["DBSnapshots"]
    )


def _bucket_exists() -> bool:
    s3 = boto3.client("s3", region_name=REGION)
    return STATE_BUCKET in {b["Name"] for b in s3.list_buckets()["Buckets"]}


def doc(result):
    return json.loads(result.stdout)


# Snapshots, then the lifting apply, then destroy


def test_snapshot_identifiers_come_before_the_first_terraform_call(project, monkeypatch):
    events = FakeTerraform.events
    with cloud(project, monkeypatch):
        data = destroy_env(
            project,
            "prod",
            confirm="prod",
            delete_data=None,
            prompt=None,
            terraform=FakeTerraform(),
            notify=lambda line: events.append(("notify", line)),
            now=datetime(2026, 10, 18, 9, 30, 0, tzinfo=UTC),
        )
        taken = _snapshots()
        assert _bucket_exists()
    expected = [
        "vecten-acme-prod-state-final-20261018093000",
        "vecten-acme-prod-warehouse-final-20261018093000",
    ]
    assert taken == expected
    assert sorted(data["snapshots"]) == expected
    first_terraform = next(i for i, e in enumerate(events) if e[0] == "terraform")
    notified = [i for i, e in enumerate(events) if e[0] == "notify" and "final-" in e[1]]
    assert len(notified) == 2 and max(notified) < first_terraform
    for identifier in expected:
        assert any(identifier in e[1] for e in events[:first_terraform] if e[0] == "notify")
    assert FakeTerraform.calls() == [
        ("init", "aws"),
        ("state", "aws"),
        ("plan", "aws"),
        ("show", "aws"),
        ("show", "aws"),
        ("apply", "aws"),
        ("destroy", "aws"),
    ]
    plan = FakeTerraform.call("plan")
    assert "destroying=true" in _vars(plan)
    # Only what the state holds is targeted; the VPC is left to the destroy.
    assert [a for a in plan if a.startswith("-target=")] == [
        f"-target={STATE_INSTANCE}",
        f"-target={STATE_PARAMS}",
        f"-target={WAREHOUSE_INSTANCE}",
        f"-target={BUCKET_ADDRESS}",
    ]
    destroy = FakeTerraform.call("destroy")
    assert "-auto-approve" in destroy
    assert "-var-file=../../envs/prod.tfvars" in destroy
    assert "destroying=true" in _vars(destroy)
    init = FakeTerraform.call("init")
    assert f"-backend-config=bucket={STATE_BUCKET}" in init
    assert "-backend-config=key=prod/aws.tfstate" in init
    assert data["state_bucket"] == STATE_BUCKET
    assert data["destroyed"] == ["aws"]


def test_confirmed_destroy_prints_snapshots_and_keeps_the_state_bucket(
    invoke, project, monkeypatch
):
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
        assert r.exit_code == 0, r.stdout
        assert _bucket_exists()
        s3 = boto3.client("s3", region_name=REGION)
        keys = [o["Key"] for o in s3.list_objects_v2(Bucket=STATE_BUCKET).get("Contents", [])]
    data = doc(r)
    assert len(data["snapshots"]) == 2
    for identifier in data["snapshots"]:
        assert identifier.startswith("vecten-acme-prod-")
        assert r.stderr.count(identifier) == 1
    # RDS keeps a manual snapshot until someone deletes it.
    assert data["snapshot_expiry"] == {}
    assert "expire" not in r.stderr
    assert data["data_deleted"] is False
    # The ship workflow reads the outputs object; a destroyed environment has none.
    assert "prod/outputs.json" not in keys
    facts = load_config(project).environments["prod"]
    assert facts.deployed_commit is None
    assert facts.bootstrapped is True and facts.state_bucket == STATE_BUCKET


def test_human_mode_prints_each_snapshot_once(invoke, project, monkeypatch):
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod")
    assert r.exit_code == 0, r.stderr
    both = r.stdout + r.stderr
    assert both.count("vecten-acme-prod-warehouse-final-") == 1
    assert "vecten-acme-prod-warehouse-final-" in r.stderr
    assert STATE_BUCKET in r.stdout


def test_minimal_environment_snapshots_its_one_instance(invoke, project, monkeypatch):
    with cloud(project, monkeypatch, outputs=minimal_outputs()):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert [s.rsplit("-final-", 1)[0] for s in doc(r)["snapshots"]] == ["vecten-acme-prod-state"]


def test_delete_data_takes_no_snapshot(invoke, project, monkeypatch):
    _add_env(project, "sandbox")
    with cloud(project, monkeypatch, env="sandbox"):
        r = invoke(
            "destroy",
            "--env",
            "sandbox",
            "--confirm",
            "sandbox",
            "--delete-data",
            "sandbox",
            "--json",
        )
        assert _snapshots() == []
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["snapshots"] == [] and data["data_deleted"] is True
    assert ("destroy", "aws") in FakeTerraform.calls()
    assert "-backend-config=key=sandbox/aws.tfstate" in FakeTerraform.call("init")


def test_an_instance_already_gone_is_reported_not_snapshotted(project, monkeypatch):
    with cloud(project, monkeypatch):
        boto3.client("rds", region_name=REGION).delete_db_instance(
            DBInstanceIdentifier="vecten-acme-prod-warehouse", SkipFinalSnapshot=True
        )
        data = destroy_env(
            project,
            "prod",
            confirm="prod",
            delete_data=None,
            prompt=None,
            terraform=FakeTerraform(),
        )
    assert [s.rsplit("-final-", 1)[0] for s in data["snapshots"]] == ["vecten-acme-prod-state"]
    assert data["instances_absent"] == ["vecten-acme-prod-warehouse"]


# Confirmation


def test_typed_back_name_confirms(invoke, project, monkeypatch):
    monkeypatch.setattr(deploy_cmd, "_stdin_is_tty", lambda: True)
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", input="prod\n")
    assert r.exit_code == 0, r.stderr
    assert r.stderr.count("final snapshot vecten-acme-prod-") == 2
    assert "type prod to destroy it" in r.stderr


def test_wrong_typed_name_destroys_nothing(invoke, project, monkeypatch):
    monkeypatch.setattr(deploy_cmd, "_stdin_is_tty", lambda: True)
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", input="prod2\n")
        assert _snapshots() == []
    assert r.exit_code == 4
    assert "nothing was destroyed" in r.stderr
    assert FakeTerraform.events == []


def test_wrong_confirm_destroys_nothing(invoke, project, monkeypatch):
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "staging", "--json")
        assert _snapshots() == []
    assert r.exit_code == 4
    assert FakeTerraform.events == []


def test_no_terminal_and_no_confirm_is_usage(invoke, project, monkeypatch):
    monkeypatch.setattr(deploy_cmd, "_stdin_is_tty", lambda: False)
    r = invoke("destroy", "--env", "prod", "--json")
    assert r.exit_code == 2
    assert "--confirm prod" in doc(r)["error"]
    assert FakeTerraform.events == []


def test_delete_data_naming_another_environment_is_usage(invoke, project, monkeypatch):
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--delete-data", "other", "--json")
    assert r.exit_code == 2
    assert "--delete-data" in doc(r)["error"]
    assert FakeTerraform.events == []


def test_inside_actions_without_confirm_is_refused(invoke, project, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(deploy_cmd, "_stdin_is_tty", lambda: True)
    r = invoke("destroy", "--env", "prod", "--json", input="prod\n")
    assert r.exit_code == 4
    assert "--confirm prod" in doc(r)["error"]
    assert FakeTerraform.events == []


def test_inside_actions_with_confirm_destroys(invoke, project, monkeypatch):
    set_facts(project, **STATE_FACTS)
    with cloud(project, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    # CI never writes vecten.toml.
    facts = load_config(project).environments["prod"]
    assert facts.deployed_commit == COMMIT and facts.first_user_added
    assert doc(r)["facts_cleared"] == []


STATE_FACTS = {
    "first_user_added": True,
    "admin_link_issued": True,
    "connector_access_applied": ["hubspot"],
}


def test_destroy_clears_the_facts_the_state_database_held(invoke, project, monkeypatch):
    set_facts(project, provisioner_script_run=True, **STATE_FACTS)
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    facts = load_config(project).environments["prod"]
    assert not facts.first_user_added and not facts.admin_link_issued
    assert facts.connector_access_applied == []
    # Over Postgres the provisioner lives in the warehouse the destroy snapshotted.
    assert facts.provisioner_script_run
    assert doc(r)["facts_cleared"] == list(STATE_FACTS)


def test_destroy_forgets_the_login_record(invoke, project, monkeypatch):
    write_record(project, "login/prod", {"client_id": "c"})
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert read_record(project, "login/prod") is None


def test_destroy_forgets_the_password_links(invoke, project, monkeypatch):
    added(project, "prod", "p-grace", "grace@acme.test")
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert read_links(project, "prod") == []


def test_the_message_says_next_walks_the_mcp_setup_again(invoke, project, monkeypatch):
    set_facts(project, **STATE_FACTS)
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod")
    assert r.exit_code == 0, r.stderr
    assert "empty state database" in r.stdout and "vecten next" in r.stdout


def test_a_destroy_with_nothing_to_clear_says_nothing_of_it(invoke, project, monkeypatch):
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["facts_cleared"] == []


def test_there_is_no_yes_option(invoke, project):
    r = invoke("destroy", "--help")
    assert r.exit_code == 0
    assert "--confirm" in r.stdout and "--delete-data" in r.stdout
    assert "--yes" not in r.stdout and "-y" not in r.stdout.split()


def test_destroy_needs_an_environment(invoke, project):
    r = invoke("destroy", "--json")
    assert r.exit_code == 2
    assert "--env" in doc(r)["error"]


# Refusals before any cloud call


def test_not_bootstrapped_names_bootstrap(invoke, project, monkeypatch):
    set_facts(project, bootstrapped=False, state_bucket=None)
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 4
    assert "vecten bootstrap --env prod" in doc(r)["error"]
    assert FakeTerraform.events == []


def test_missing_terraform_is_a_prerequisite(invoke, project, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "missing", True)
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
        assert _snapshots() == []
    assert r.exit_code == 3
    assert "terraform" in doc(r)["error"]


def test_missing_outputs_refuse_without_delete_data(invoke, project, monkeypatch):
    with cloud(project, monkeypatch, outputs=None):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 3
    error = doc(r)["error"]
    assert "--delete-data prod" in error
    assert FakeTerraform.events == []


LOCK = {
    "ID": "lock-1",
    "Operation": "OperationTypeApply",
    "Info": "",
    "Who": "jane@laptop",
    "Version": "1.16.0",
    "Created": "2026-10-27T08:15:33Z",
    "Path": f"{STATE_BUCKET}/prod/aws.tfstate",
}


def _hold_lock(body: bytes = json.dumps(LOCK).encode()) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    s3.put_object(Bucket=STATE_BUCKET, Key="prod/aws.tfstate.tflock", Body=body)


def test_a_held_state_lock_refuses_before_any_snapshot(invoke, project, monkeypatch):
    with cloud(project, monkeypatch):
        _hold_lock()
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
        assert _snapshots() == []
    assert r.exit_code == 4
    error = doc(r)["error"]
    assert "jane@laptop" in error and "OperationTypeApply" in error
    assert "2026-10-27T08:15:33Z" in error
    assert f"s3://{STATE_BUCKET}/prod/aws.tfstate.tflock" in error
    assert "nothing was snapshotted or destroyed" in error
    assert FakeTerraform.calls() == []


def test_an_unreadable_lock_still_refuses(invoke, project, monkeypatch):
    with cloud(project, monkeypatch):
        _hold_lock(b"not json")
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
        assert _snapshots() == []
    assert r.exit_code == 4
    assert "unknown" in doc(r)["error"]
    assert FakeTerraform.calls() == []


def test_delete_data_with_a_held_lock_refuses(invoke, project, monkeypatch):
    with cloud(project, monkeypatch):
        _hold_lock()
        r = invoke("destroy", "--env", "prod", "--delete-data", "prod", "--confirm", "prod")
    assert r.exit_code == 4
    assert FakeTerraform.calls() == []


def test_a_failed_destroy_keeps_the_facts(invoke, project, monkeypatch):
    set_facts(project, **STATE_FACTS)
    write_record(project, "login/prod", {"client_id": "c"})
    added(project, "prod", "p-grace", "grace@acme.test")
    monkeypatch.setattr(FakeTerraform, "failing", {"destroy": 1})
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
        assert len(_snapshots()) == 2
    assert r.exit_code == 5
    facts = load_config(project).environments["prod"]
    assert facts.deployed_commit == COMMIT and facts.first_user_added
    assert read_record(project, "login/prod") is not None
    assert len(read_links(project, "prod")) == 1


# Snowflake


@pytest.fixture
def snowflake(tmp_path, monkeypatch):
    root = tmp_path / "snow"
    answers = {**LOCAL, "sources": ["hubspot"], "warehouse": "snowflake", **DEPLOY, "cloud": "aws"}
    render_project(root, ScaffoldParams.from_answers(answers, {"components": ["platform", "mcp"]}))
    path = env_tfvars_path(root, "prod")
    lines = [
        'snowflake_account = "ACME-PROD"' if ln.startswith("snowflake_account") else ln
        for ln in path.read_text().splitlines()
    ]
    path.write_text("\n".join(lines) + "\n")
    config = load_config(root)
    config.warehouse.account = "ACME-PROD"
    save_config(root, config)
    set_facts(root, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:FakeTerraform")
    monkeypatch.setattr(FakeTerraform, "events", [])
    monkeypatch.setattr(FakeTerraform, "missing", False)
    monkeypatch.setattr(FakeTerraform, "failing", {})
    monkeypatch.setattr(FakeTerraform, "targets", [])
    return root


def _snowflake_outputs() -> dict:
    outputs = json.loads(json.dumps(STANDARD_OUTPUTS))
    del outputs["postgres"]["warehouse"]
    return outputs


def _put_deploy_secrets() -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    for name, value in (
        ("DEPLOY_SNOWFLAKE_USER", DEPLOY_USER_VALUE),
        ("DEPLOY_SNOWFLAKE_PRIVATE_KEY", DEPLOY_KEY_VALUE),
    ):
        sm.create_secret(Name=secret_id("acme", "prod", name), SecretString=value)


def test_snowflake_database_is_kept_without_delete_data(invoke, snowflake, monkeypatch):
    with cloud(snowflake, monkeypatch, outputs=_snowflake_outputs()):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert not any(root == "snowflake" for _, root in FakeTerraform.calls())
    data = doc(r)
    assert data["destroyed"] == ["aws"]
    assert data["snowflake"] == "kept"
    assert len(data["snapshots"]) == 1


def test_snowflake_message_says_the_database_was_kept(invoke, snowflake, monkeypatch):
    with cloud(snowflake, monkeypatch, outputs=_snowflake_outputs()):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod")
    assert r.exit_code == 0, r.stderr
    assert "Snowflake database" in r.stdout and "kept" in r.stdout


def test_snowflake_destroy_asks_for_the_provisioner_script_again(invoke, snowflake, monkeypatch):
    set_facts(snowflake, provisioner_script_run=True, first_user_added=True)
    with cloud(snowflake, monkeypatch, outputs=_snowflake_outputs()):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod")
    assert r.exit_code == 0, r.stderr
    facts = load_config(snowflake).environments["prod"]
    assert not facts.provisioner_script_run and not facts.first_user_added
    assert "PROVISIONER created stay in Snowflake" in r.stdout
    assert "vecten mcp provisioner-script --env prod" in r.stdout


def test_snowflake_root_is_destroyed_with_delete_data(invoke, snowflake, monkeypatch):
    with cloud(snowflake, monkeypatch, outputs=_snowflake_outputs()):
        _put_deploy_secrets()
        r = invoke(
            "destroy", "--env", "prod", "--confirm", "prod", "--delete-data", "prod", "--json"
        )
        sm = boto3.client("secretsmanager", region_name=REGION)
        names = [x["Name"] for x in sm.list_secrets()["SecretList"]]
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["snowflake"] == "destroyed"
    assert data["destroyed"] == ["snowflake", "aws"]
    assert ("destroy", "snowflake") in FakeTerraform.calls()
    assert ("plan", "snowflake") not in FakeTerraform.calls()
    snow_destroy = FakeTerraform.call("destroy", "snowflake")
    assert "destroying=true" not in _vars(snow_destroy)
    assert "loader_public_key=-----BEGIN PUBLIC KEY-----" in "\n".join(_vars(snow_destroy))
    assert DEPLOY_KEY_VALUE not in r.stdout
    # A destroy reads secrets and never writes one: the throwaway loader key is not stored.
    assert secret_id("acme", "prod", "SNOWFLAKE_LOADER_PRIVATE_KEY") not in names


@pytest.mark.parametrize(("delete_data", "kept"), [(True, None), (False, False)])
def test_only_data_deletion_forgets_the_snowflake_layout(
    invoke, snowflake, monkeypatch, delete_data, kept
):
    set_facts(snowflake, snowflake_shared_account=False)
    args = ["--delete-data", "prod"] if delete_data else []
    with cloud(snowflake, monkeypatch, outputs=_snowflake_outputs()):
        _put_deploy_secrets()
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", *args, "--json")
    assert r.exit_code == 0, r.stdout
    assert load_config(snowflake).environments["prod"].snowflake_shared_account is kept


def test_a_kept_environment_deployed_without_a_record_keeps_its_fixed_names(
    invoke, snowflake, monkeypatch
):
    # Deployed before layouts were recorded, so its objects carry the fixed names.
    config = load_config(snowflake)
    config.warehouse.shared_account = True
    save_config(snowflake, config)
    set_facts(snowflake, provisioner_script_run=True)
    with cloud(snowflake, monkeypatch, outputs=_snowflake_outputs()):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod")
    assert r.exit_code == 0, r.stderr
    facts = load_config(snowflake).environments["prod"]
    assert facts.deployed_commit is None and facts.snowflake_shared_account is False
    assert "PROVISIONER created stay in Snowflake" in r.stdout
    assert "PROVISIONER_ACME_PROD" not in r.stdout


def test_postgres_project_reports_no_snowflake_database(invoke, project, monkeypatch):
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["snowflake"] is None


# The lifting apply


def test_a_lifting_plan_that_creates_is_refused(invoke, project, monkeypatch):
    # A dependency of a target in state is missing, so the targeted plan would create it.
    creates = [
        *UPDATE_ONLY,
        {
            "address": "module.environment.aws_db_subnet_group.x",
            "change": {"actions": ["create"]},
            "needed_by": STATE_INSTANCE,
        },
    ]
    monkeypatch.setattr(FakeTerraform, "changes", creates)
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 4
    assert "module.environment.aws_db_subnet_group.x" in doc(r)["error"]
    commands = [c for c, _ in FakeTerraform.calls()]
    assert "apply" not in commands and "destroy" not in commands
    assert load_config(project).environments["prod"].deployed_commit == COMMIT


def test_a_lifting_plan_that_replaces_is_refused(invoke, project, monkeypatch):
    replaces = [{"address": STATE_INSTANCE, "change": {"actions": ["delete", "create"]}}]
    monkeypatch.setattr(FakeTerraform, "changes", replaces)
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 4
    assert "apply" not in [c for c, _ in FakeTerraform.calls()]


def test_instances_already_gone_skip_the_lift_and_destroy_the_rest(invoke, project, monkeypatch):
    # A partial destroy, or instances deleted out of band: the state holds the VPC alone.
    monkeypatch.setattr(FakeTerraform, "state", {"aws": [VPC]})
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.calls() == [("init", "aws"), ("state", "aws"), ("destroy", "aws")]
    assert doc(r)["destroyed"] == ["aws"]


def test_an_instance_gone_from_state_is_not_targeted(invoke, project, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "state", {"aws": [VPC, STATE_INSTANCE, BUCKET_ADDRESS]})
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    targets = [a for a in FakeTerraform.call("plan") if a.startswith("-target=")]
    assert targets == [f"-target={STATE_INSTANCE}", f"-target={BUCKET_ADDRESS}"]
    assert "apply" in [c for c, _ in FakeTerraform.calls()]


def test_a_no_op_lifting_plan_skips_the_apply(invoke, project, monkeypatch):
    monkeypatch.setattr(
        FakeTerraform,
        "changes",
        [{"address": STATE_INSTANCE, "change": {"actions": ["no-op"]}}],
    )
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    commands = [c for c, _ in FakeTerraform.calls()]
    assert "apply" not in commands
    assert commands[-1] == "destroy"


def test_an_empty_state_is_reported_already_destroyed(invoke, project, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "state", {})
    with cloud(project, monkeypatch):
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.calls() == [("init", "aws"), ("state", "aws")]
    data = doc(r)
    assert data["destroyed"] == [] and data["already_destroyed"] == ["aws"]


def test_json_without_confirm_never_prompts(invoke, project, monkeypatch):
    monkeypatch.setattr(deploy_cmd, "_stdin_is_tty", lambda: True)
    r = invoke("destroy", "--env", "prod", "--json", input="prod\n")
    assert r.exit_code == 2
    assert "--confirm prod" in doc(r)["error"]
    assert FakeTerraform.events == []


def test_a_failed_outputs_clean_up_still_reports_the_destroy(invoke, project, monkeypatch):
    from vecten.deploy.aws import clients

    real = clients.client

    class RefusingS3:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def delete_object(self, **kwargs):
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "DeleteObject")

    with cloud(project, monkeypatch):
        monkeypatch.setattr(
            clients,
            "client",
            lambda svc, region: RefusingS3(real(svc, region)) if svc == "s3" else real(svc, region),
        )
        r = invoke("destroy", "--env", "prod", "--confirm", "prod")
    assert r.exit_code == 0, r.stderr
    assert "the infrastructure is destroyed" in r.stdout
    assert f"s3://{STATE_BUCKET}/prod/outputs.json" in r.stdout
    assert load_config(project).environments["prod"].deployed_commit is None


# An instance deleted outside Terraform stays in state; a refreshed plan would create it.
RECREATES_WAREHOUSE = [
    {"address": STATE_INSTANCE, "change": {"actions": ["update"]}},
    {"address": WAREHOUSE_INSTANCE, "change": {"actions": ["create"]}},
    {"address": BUCKET_ADDRESS, "change": {"actions": ["update"]}},
]


def _delete_instances(*keys: str) -> None:
    rds = boto3.client("rds", region_name=REGION)
    for key in keys:
        rds.delete_db_instance(
            DBInstanceIdentifier=f"vecten-acme-prod-{key}", SkipFinalSnapshot=True
        )


def _targets() -> list[str]:
    return [a for a in FakeTerraform.call("plan") if a.startswith("-target=")]


def test_an_instance_deleted_out_of_band_is_not_targeted(invoke, project, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "changes", RECREATES_WAREHOUSE)
    with cloud(project, monkeypatch):
        _delete_instances("warehouse")
        r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert _targets() == [
        f"-target={STATE_INSTANCE}",
        f"-target={STATE_PARAMS}",
        f"-target={BUCKET_ADDRESS}",
    ]
    assert [c for c, _ in FakeTerraform.calls()][-2:] == ["apply", "destroy"]
    data = doc(r)
    assert data["instances_absent"] == ["vecten-acme-prod-warehouse"]
    assert [s.rsplit("-final-", 1)[0] for s in data["snapshots"]] == ["vecten-acme-prod-state"]


def test_delete_data_asks_aws_which_instances_are_gone(invoke, project, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "changes", RECREATES_WAREHOUSE)
    with cloud(project, monkeypatch):
        _delete_instances("warehouse")
        r = invoke(
            "destroy", "--env", "prod", "--confirm", "prod", "--delete-data", "prod", "--json"
        )
    assert r.exit_code == 0, r.stdout
    assert f"-target={WAREHOUSE_INSTANCE}" not in _targets()
    assert f"-target={STATE_INSTANCE}" in _targets()
    assert [c for c, _ in FakeTerraform.calls()][-1] == "destroy"
    data = doc(r)
    assert data["snapshots"] == []
    assert data["instances_absent"] == ["vecten-acme-prod-warehouse"]


def test_every_instance_gone_with_delete_data_skips_the_lift(invoke, project, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "state", {"aws": [VPC, STATE_INSTANCE, WAREHOUSE_INSTANCE]})
    monkeypatch.setattr(FakeTerraform, "changes", RECREATES_WAREHOUSE)
    with cloud(project, monkeypatch):
        _delete_instances("state", "warehouse")
        r = invoke(
            "destroy", "--env", "prod", "--confirm", "prod", "--delete-data", "prod", "--json"
        )
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.calls() == [("init", "aws"), ("state", "aws"), ("destroy", "aws")]


# Scaleway, from the laptop with the human's key


SCW_BUCKET = "module.environment.scaleway_object_bucket.artifacts"
SCW_STATE_RDB = 'module.environment.module.postgres["state"].scaleway_rdb_instance.this'
SCW_WAREHOUSE_RDB = 'module.environment.module.postgres["warehouse"].scaleway_rdb_instance.this'
SCW_NETWORK = "module.environment.scaleway_vpc_private_network.this"
SCW_STATE = {"scaleway": [SCW_NETWORK, SCW_STATE_RDB, SCW_WAREHOUSE_RDB, SCW_BUCKET]}
SCW_CHANGES = [
    {"address": SCW_BUCKET, "change": {"actions": ["update"]}},
    {"address": SCW_NETWORK, "change": {"actions": ["no-op"]}},
]


@pytest.fixture
def scaleway(tmp_path, monkeypatch, project):
    from vecten.deploy.scaleway import snapshots as scw_snapshots

    root = render_aws(tmp_path / "scw", cloud="scaleway")
    monkeypatch.chdir(root)
    org, storage, _ = install(monkeypatch, tmp_path)
    org.log = storage.log = FakeTerraform.events
    outputs = prepare_environment(root, org, storage, scaleway_outputs(), deployed_commit=COMMIT)
    monkeypatch.setattr(FakeTerraform, "state", SCW_STATE)
    monkeypatch.setattr(FakeTerraform, "changes", SCW_CHANGES)
    time = FakeTime()
    monkeypatch.setattr(scw_snapshots, "sleep", time.sleep)
    return type("Scw", (), {"root": root, "org": org, "storage": storage, "outputs": outputs})


def test_scaleway_destroy_snapshots_then_lets_the_bucket_go_then_destroys(invoke, scaleway):
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    names = data["snapshots"]
    assert [n.rsplit("-final-", 1)[0] for n in names] == [
        "vecten-acme-prod-state",
        "vecten-acme-prod-warehouse",
    ]
    assert {s["name"] for s in scaleway.org.snapshots.values()} == set(names)
    events = FakeTerraform.events
    first_terraform = next(i for i, e in enumerate(events) if e[0] == "terraform")
    ready = [
        i for i, e in enumerate(events) if e[:3] == ("rest", "rdb", "GET") and "/snapshots/" in e[3]
    ]
    assert ready and max(ready) < first_terraform
    assert FakeTerraform.calls() == [
        ("init", "scaleway"),
        ("state", "scaleway"),
        ("plan", "scaleway"),
        ("show", "scaleway"),
        ("show", "scaleway"),
        ("apply", "scaleway"),
        ("destroy", "scaleway"),
    ]
    plan = FakeTerraform.call("plan", "scaleway")
    assert [a for a in plan if a.startswith("-target=")] == [f"-target={SCW_BUCKET}"]
    assert "destroying=true" in _vars(plan)
    destroy = FakeTerraform.call("destroy", "scaleway")
    assert "destroying=true" in _vars(destroy)
    assert any(v.startswith("project_id=") for v in _vars(destroy))
    assert scaleway.storage.body(scw_state_bucket(), "prod/outputs.json") is None
    assert data["destroyed"] == ["scaleway"]
    assert load_config(scaleway.root).environments["prod"].deployed_commit is None


def test_scaleway_destroy_says_when_the_snapshots_expire(invoke, scaleway):
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    expiry = scaleway.org.snapshot_default_expires_at
    assert data["snapshot_expiry"] == {name: expiry for name in data["snapshots"]}


def test_scaleway_destroy_message_names_the_expiry_date_once(invoke, scaleway):
    r = invoke("destroy", "--env", "prod", "--confirm", "prod")
    assert r.exit_code == 0, r.stderr
    both = r.stdout + r.stderr
    assert both.count("2027-10-18") == 1
    line = next(line for line in r.stdout.splitlines() if "2027-10-18" in line)
    assert "restore" in line and "new snapshot" in line
    assert both.count("vecten-acme-prod-warehouse-final-") == 1


def test_scaleway_destroy_whose_snapshot_fails_destroys_nothing(invoke, scaleway):
    scaleway.org.snapshot_final_status = "error"
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 5
    assert "nothing was destroyed" in doc(r)["error"]
    assert FakeTerraform.calls() == []


def test_scaleway_destroy_with_the_bucket_already_gone_skips_the_lift(
    invoke, scaleway, monkeypatch
):
    monkeypatch.setattr(FakeTerraform, "state", {"scaleway": [SCW_NETWORK]})
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert FakeTerraform.calls() == [
        ("init", "scaleway"),
        ("state", "scaleway"),
        ("destroy", "scaleway"),
    ]


def _key_removals(events) -> list[int]:
    return [
        i
        for i, e in enumerate(events)
        if e[:3] == ("rest", "instance", "DELETE") and e[3].endswith("/user_data/vecten-key")
    ]


def test_scaleway_destroy_sweeps_leftover_keys_before_terraform_refreshes(invoke, scaleway):
    server = next(iter(scaleway.org.servers.values()))
    server["user_data"]["vecten-key"] = "SCWLEFTOVER:secret"
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert all(not s["user_data"] for s in scaleway.org.servers.values())
    events = FakeTerraform.events
    refreshing = [
        i for i, e in enumerate(events) if e[0] == "terraform" and e[1][0] in ("plan", "destroy")
    ]
    removals = _key_removals(events)
    assert len(removals) == len(scaleway.org.servers)
    assert max(removals) < min(refreshing)


def test_scaleway_destroy_without_the_bucket_still_sweeps_keys(invoke, scaleway, monkeypatch):
    monkeypatch.setattr(FakeTerraform, "state", {"scaleway": [SCW_NETWORK]})
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    events = FakeTerraform.events
    destroy = next(i for i, e in enumerate(events) if e[0] == "terraform" and e[1][0] == "destroy")
    assert _key_removals(events) and max(_key_removals(events)) < destroy


def test_scaleway_destroy_stops_when_a_leftover_key_stays(invoke, scaleway):
    server_id = next(iter(scaleway.org.servers))
    scaleway.org.failures[("instance", "DELETE", f"/servers/{server_id}")] = (
        403,
        "permissions_denied",
    )
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 5, r.stdout
    assert "vecten-key" in doc(r)["error"]
    assert not [c for c in FakeTerraform.calls() if c[0] in ("plan", "apply", "destroy")]


def test_scaleway_destroy_with_a_held_lock_refuses(invoke, scaleway):
    lock = json.dumps({**LOCK, "Path": f"{scw_state_bucket()}/prod/scaleway.tfstate"})
    scaleway.storage.put(scw_state_bucket(), "prod/scaleway.tfstate.tflock", lock.encode())
    r = invoke("destroy", "--env", "prod", "--confirm", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert f"s3://{scw_state_bucket()}/prod/scaleway.tfstate.tflock" in doc(r)["error"]
    assert scaleway.org.snapshots == {}
    assert FakeTerraform.calls() == []
