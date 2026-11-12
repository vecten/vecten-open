import json

import boto3
import pytest
from moto import mock_aws

from vecten.deploy import github_env
from vecten.deploy.aws import clients
from vecten.deploy.aws import ship as ship_mod
from vecten.github import gh as gh_mod

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
from .scaleway_fakes import (
    FakeTime,
    FakeWatcher,
    actions_key,
    install,
    prepare_environment,
    scaleway_outputs,
)


class RecordingTerraform:
    created = 0

    def __init__(self):
        type(self).created += 1

    def which(self):
        raise AssertionError("ship ran terraform")

    def run(self, *args, **kwargs):
        raise AssertionError("ship ran terraform")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "proj")
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:RecordingTerraform")
    RecordingTerraform.created = 0
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    return root


@pytest.fixture
def no_cloud(project, monkeypatch):
    no_aws_credentials(monkeypatch, project)

    def refuse(service, region):
        raise AssertionError(f"a {service} client was created")

    monkeypatch.setattr(clients, "client", refuse)
    return project


def doc(result):
    return json.loads(result.stdout)


def test_ship_needs_an_environment(invoke, project):
    r = invoke("ship", "--json")
    assert r.exit_code == 2
    assert "--env" in doc(r)["error"]


def test_ship_before_deploy_names_vecten_deploy(invoke, no_cloud):
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "vecten deploy --env prod" in doc(r)["error"]


def test_check_reports_facts_without_credentials(invoke, no_cloud, tmp_path, monkeypatch):
    out = tmp_path / "github_output"
    out.write_text("earlier=1\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r) == {"bootstrapped": False, "deployed": False, "ok": True}
    assert out.read_text() == "earlier=1\nbootstrapped=false\ndeployed=false\n"

    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert doc(r) == {"bootstrapped": True, "deployed": True, "ok": True}
    assert out.read_text().endswith("bootstrapped=true\ndeployed=true\n")


def test_check_without_github_output_only_prints(invoke, no_cloud):
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == 0
    assert doc(r) == {"bootstrapped": False, "deployed": False, "ok": True}


def test_check_on_a_project_without_a_deploy_layer(invoke, tmp_path, monkeypatch):
    from vecten.scaffold.params import ScaffoldParams
    from vecten.scaffold.render import render_project

    from .scaffold_matrix import LOCAL

    root = tmp_path / "local"
    render_project(root, ScaffoldParams.from_answers({**LOCAL, "sources": ["hubspot"]}, {}))
    monkeypatch.chdir(root)
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == 0
    assert doc(r) == {"bootstrapped": False, "deployed": False, "ok": True}


def test_outside_actions_dispatches_the_ship_workflow_and_follows_it(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    dispatch = gh.named("workflow", "run")
    assert len(dispatch) == 1
    assert dispatch[0][:7] == [
        "workflow",
        "run",
        "deploy-platform.yml",
        "--ref",
        "main",
        "-f",
        "environment=prod",
    ]
    assert gh.named("run", "watch")[0][:4] == ["run", "watch", "42", "--exit-status"]
    data = doc(r)
    assert data["dispatched"] is True
    assert data["run_id"] == 42
    assert data["head_sha"] == COMMIT
    assert data["conclusion"] == "success"
    assert RecordingTerraform.created == 0


def test_laptop_message_says_when_local_commits_were_not_shipped(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh())
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit(head="f" * 40))
    r = invoke("ship", "--env", "prod")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert f"CI shipped main at {COMMIT[:8]}" in r.stdout
    assert "local commits were not included" in r.stdout


def test_laptop_message_when_local_head_was_shipped(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh())
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod")
    assert r.exit_code == 0
    assert "not included" not in r.stdout
    assert COMMIT[:8] in r.stdout


def test_a_failed_watched_run_exits_5_with_its_url(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(watch_code=1))
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 5
    assert "https://github.com/acme/data/actions/runs/42" in doc(r)["error"]


def test_a_run_that_never_appears_exits_5(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(created_at="2000-01-01T00:00:00Z"))
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 5
    assert "deploy-platform.yml" in doc(r)["error"]


def test_inside_actions_ships_without_terraform(invoke, project, monkeypatch):
    aws_credentials(monkeypatch, project)
    set_facts(project, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    ssm = FakeSsm()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (BUCKET, STATE_BUCKET):
            s3.create_bucket(
                Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION}
            )
        s3.put_object(
            Bucket=STATE_BUCKET, Key="prod/outputs.json", Body=json.dumps(STANDARD_OUTPUTS)
        )
        real = clients.client
        monkeypatch.setattr(
            clients, "client", lambda svc, region: ssm if svc == "ssm" else real(svc, region)
        )
        monkeypatch.setattr(ship_mod, "poll", lambda url, timeout, **kw: True)
        r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["hosts"] == ["mcp", "platform"]
    assert RecordingTerraform.created == 0


@pytest.fixture
def scaleway(tmp_path, monkeypatch):
    """A deployed Scaleway environment inside Actions with the deploy key; every VM's watcher
    answers its pointer and both components answer healthy."""
    from vecten.deploy.scaleway import ship as scw_ship

    root = render_aws(tmp_path / "scw", cloud="scaleway")
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:RecordingTerraform")
    RecordingTerraform.created = 0
    org, storage, _ = install(monkeypatch, tmp_path)
    actions_key(monkeypatch)
    outputs = prepare_environment(root, org, storage, scaleway_outputs(), deployed_commit=COMMIT)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    time = FakeTime()
    monkeypatch.setattr(scw_ship, "sleep", time.sleep)
    monkeypatch.setattr(scw_ship, "clock", time.clock)
    monkeypatch.setattr(scw_ship, "poll", lambda url, timeout, **kw: True)
    return type(
        "Scw",
        (),
        {"root": root, "storage": storage, "watcher": FakeWatcher(org, storage, outputs)},
    )


def test_scaleway_ship_moves_each_pointer_and_waits_for_its_watcher(invoke, scaleway):
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["hosts"] == ["mcp", "platform"]
    assert RecordingTerraform.created == 0
    # Neither host has a pointer yet, so both ships are forced.
    assert [(h, d["commit"], d["force"]) for h, d in scaleway.watcher.pointers] == [
        ("mcp", COMMIT, True),
        ("platform", COMMIT, True),
    ]


def test_scaleway_ship_of_an_unchanged_commit_forces_a_reship(invoke, scaleway):
    assert invoke("ship", "--env", "prod", "--json").exit_code == 0
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    second = scaleway.watcher.pointers[2:]
    assert [(h, d["force"]) for h, d in second] == [("mcp", True), ("platform", True)]


def test_scaleway_ship_whose_watcher_never_answers_exits_5_naming_the_host(invoke, scaleway):
    scaleway.watcher.silent.add("mcp")
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 5
    error = doc(r)["error"]
    assert "host mcp" in error and "watcher did not pick up" in error


@pytest.fixture
def snowflake_deployed(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "snow", warehouse="snowflake", snowflake_account="ACME-PROD")
    set_facts(root, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:RecordingTerraform")
    no_aws_credentials(monkeypatch, root)
    return root


def test_a_pending_snowflake_identity_is_not_dispatched_from_the_laptop(
    invoke, snowflake_deployed, monkeypatch
):
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    assert "vecten mcp provisioner-script --env prod" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_a_pending_snowflake_identity_ships_nothing_in_actions(
    invoke, snowflake_deployed, monkeypatch
):
    aws_credentials(monkeypatch, snowflake_deployed)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    ssm = FakeSsm()
    monkeypatch.setattr(clients, "client", lambda svc, region: ssm)
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["hosts"] == [] and ssm.sent == []
    assert "vecten mcp provisioner-script --env prod" in doc(r)["ship_skipped"]


def test_check_refuses_a_setting_of_the_wrong_type_for_the_environment(invoke, no_cloud):
    from vecten_shared.output import ExitCode

    from .settings_fixtures import with_settings

    with_settings(no_cloud, {"env": {"prod": {"daily_cap": "500"}}})
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert "settings.acme.daily_cap" in r.stdout and "prod" in r.stdout
