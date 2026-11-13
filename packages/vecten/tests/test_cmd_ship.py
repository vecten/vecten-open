import json
import subprocess

import boto3
import pytest
from moto import mock_aws

from vecten.deploy import github_env
from vecten.deploy.aws import clients
from vecten.deploy.aws import ship as ship_mod
from vecten.deploy.tfvars import env_tfvars_path
from vecten.github import gh as gh_mod
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
def status_gh(monkeypatch):
    """The gh a ship inside Actions sets its commit statuses through."""
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    return gh


@pytest.fixture
def project(tmp_path, monkeypatch, status_gh):
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


# A complete GitHub App setup, its names spread over the repository and the environment.
APP = {
    "variable": ["VECTEN_GIT_APP_ID", "VECTEN_GIT_APP_OWNER"],
    "variable --env prod": ["VECTEN_GIT_APP_REPOSITORIES"],
    "secret": ["VECTEN_GIT_APP_PRIVATE_KEY"],
}


def _private_git_source(root):
    from vecten_shared.config import load_config

    from .test_git_ship import NAME, git_project

    config = load_config(root)
    config.extensions = [NAME]
    git_project(root, config)
    return NAME


def test_a_private_git_source_without_a_ci_credential_is_not_dispatched(
    invoke, no_cloud, monkeypatch
):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    name = _private_git_source(no_cloud)
    gh = FakeGh(listed={"secret": ["OTHER"], "variable --env prod": ["VECTEN_AWS_ROLE_ARN"]})
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code != 0
    error = doc(r)["error"]
    assert name in error
    assert "VECTEN_GIT_CREDENTIALS" in error and "VECTEN_GIT_APP_ID" in error
    assert gh.named("workflow", "run") == []
    # Names only: no call reads a value.
    assert all(call[1] == "list" or call[0] != "secret" for call in gh.calls)


@pytest.mark.parametrize(
    "listed",
    [
        {"secret": ["VECTEN_GIT_CREDENTIALS"]},
        {"secret --env prod": ["VECTEN_GIT_CREDENTIALS"]},
        {
            "variable": ["VECTEN_GIT_APP_ID", "VECTEN_GIT_APP_OWNER"],
            "variable --env prod": ["VECTEN_GIT_APP_REPOSITORIES"],
            "secret": ["VECTEN_GIT_APP_PRIVATE_KEY"],
        },
        {
            "variable --env prod": [
                "VECTEN_GIT_APP_ID",
                "VECTEN_GIT_APP_OWNER",
                "VECTEN_GIT_APP_REPOSITORIES",
            ],
            "secret --env prod": ["VECTEN_GIT_APP_PRIVATE_KEY"],
        },
    ],
    ids=["repository-secret", "environment-secret", "split-app", "environment-app"],
)
def test_a_private_git_source_with_a_ci_credential_is_dispatched(
    invoke, no_cloud, monkeypatch, listed
):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    _private_git_source(no_cloud)
    gh = FakeGh(listed=listed)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert len(gh.named("workflow", "run")) == 1


@pytest.mark.parametrize(
    "missing",
    ["VECTEN_GIT_APP_PRIVATE_KEY", "VECTEN_GIT_APP_OWNER", "VECTEN_GIT_APP_REPOSITORIES"],
)
def test_an_incomplete_github_app_is_refused_naming_what_is_missing(
    invoke, no_cloud, monkeypatch, missing
):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    _private_git_source(no_cloud)
    listed = {key: [n for n in names if n != missing] for key, names in APP.items()}
    gh = FakeGh(listed=listed)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code != 0
    error = doc(r)["error"]
    assert missing in error
    assert gh.named("workflow", "run") == []


def test_a_list_a_writer_may_not_read_is_skipped_with_a_warning(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    _private_git_source(no_cloud)
    gh = FakeGh(forbidden=("secret", "secret --env prod"))
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert len(gh.named("workflow", "run")) == 1
    [warning] = doc(r)["warnings"]
    assert "secret" in warning and "VECTEN_GIT_CREDENTIALS" in warning
    plain = invoke("ship", "--env", "prod")
    assert "warning:" in plain.stdout and "VECTEN_GIT_CREDENTIALS" in plain.stdout


def test_an_organisation_secret_shared_with_the_repository_counts(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    _private_git_source(no_cloud)
    gh = FakeGh()
    run = gh.run

    def answer(args, cwd, input=None):
        if args[:2] == ["api", "repos/acme/data/actions/organization-secrets"]:
            gh.calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "VECTEN_GIT_CREDENTIALS\n", "")
        return run(args, cwd, input=input)

    gh.run = answer
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout


def test_a_public_git_source_or_none_needs_no_ci_credential_check(invoke, no_cloud, monkeypatch):
    from dataclasses import replace

    from vecten_shared.config import load_config

    from .test_git_ship import git_project, origin

    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    config = load_config(no_cloud)
    config.extensions = ["acme-resolution"]
    git_project(no_cloud, config, replace(origin(), public=True))
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert gh.named("secret", "list") == [] and gh.named("variable", "list") == []


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
def scaleway(tmp_path, monkeypatch, status_gh):
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
def snowflake_deployed(tmp_path, monkeypatch, status_gh):
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


def _rename_shared(root, database="lake", *, provisioner_script_run=True):
    """A shared environment whose first deploy recorded the base ACME, with [warehouse]
    database now set to `database`."""
    config = load_config(root)
    config.warehouse.shared_account = True
    config.warehouse.database = database
    facts = config.environments["prod"]
    facts.snowflake_shared_account = True
    facts.snowflake_base = "ACME"
    facts.provisioner_script_run = provisioner_script_run
    save_config(root, config)


def test_check_refuses_a_shared_environment_whose_base_changed(
    invoke, snowflake_deployed, tmp_path, monkeypatch
):
    # The ship workflow's gate runs --check; refusing there ships no host.
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _rename_shared(snowflake_deployed)
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert "'ACME'" in error and "'LAKE'" in error and "[warehouse] database" in error
    assert not out.exists() or "deployed=true" not in out.read_text()


def test_a_shared_environment_whose_base_changed_is_not_dispatched(
    invoke, snowflake_deployed, monkeypatch
):
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    _rename_shared(snowflake_deployed)
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "'LAKE'" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_a_shared_environment_whose_base_changed_ships_no_host_in_actions(
    invoke, snowflake_deployed, monkeypatch
):
    aws_credentials(monkeypatch, snowflake_deployed)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    ssm = FakeSsm()
    monkeypatch.setattr(clients, "client", lambda svc, region: ssm)
    _rename_shared(snowflake_deployed)
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "'LAKE'" in doc(r)["error"]
    assert ssm.sent == []


def test_a_shared_environment_keeping_its_base_passes_the_check(
    invoke, snowflake_deployed, tmp_path, monkeypatch
):
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    _rename_shared(snowflake_deployed, database="acme")
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == 0, r.stdout
    assert "deployed=true" in out.read_text()


def test_a_changed_layout_is_refused_before_any_dispatch(invoke, snowflake_deployed, monkeypatch):
    dev = env_tfvars_path(snowflake_deployed, "prod").read_text().replace('"prod"', '"dev"', 1)
    env_tfvars_path(snowflake_deployed, "dev").write_text(dev)
    set_facts(snowflake_deployed, snowflake_shared_account=False, provisioner_script_run=True)
    config = load_config(snowflake_deployed)
    config.warehouse.shared_account = True
    save_config(snowflake_deployed, config)
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert "snowflake_shared_account = false" in error and "new deployment" in error
    assert gh.named("workflow", "run") == []


def test_check_refuses_a_setting_of_the_wrong_type_for_the_environment(invoke, no_cloud):
    from vecten_shared.output import ExitCode

    from .settings_fixtures import with_settings

    with_settings(no_cloud, {"env": {"prod": {"daily_cap": "500"}}})
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert "settings.acme.daily_cap" in r.stdout and "prod" in r.stdout


def test_check_refuses_a_binding_the_record_does_not_declare(invoke, no_cloud):
    from vecten_shared.output import ExitCode

    from .services_fixtures import with_services

    with_services(no_cloud, {"portal": {}})
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert "services.acme.portal" in r.stdout


def test_check_names_what_services_reserve_on_each_host(invoke, no_cloud, tmp_path, monkeypatch):
    from .services_fixtures import BOT, with_services

    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    tool = {**BOT, "routes": {}, "components": ["mcp"], "memory_mb": 128, "cpus": 0.25}
    with_services(
        no_cloud,
        {"bot": {"memory_mb": 512}, "tool": {"host": "mcp"}, "worker": {"local": False}},
        declared={"bot": {**BOT, "routes": {}}, "tool": tool, "worker": {**BOT, "routes": {}}},
    )
    r = invoke("ship", "--env", "prod", "--check", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["service_limits"] == {
        "platform": {"memory_mb": 768, "cpus": 1.0},
        "mcp": {"memory_mb": 128, "cpus": 0.25},
    }
    assert out.read_text() == "bootstrapped=false\ndeployed=false\n"
    text = invoke("ship", "--env", "prod", "--check").stdout
    assert "services on platform reserve 768 MB and 1 CPUs" in text
    assert "bootstrapped=false deployed=false" in text


# Services: reported after the component polls, never rolled back for.


def _ship_with_services(invoke, project, monkeypatch, answers, outputs=None):
    from vecten.deploy import shipping

    from .services_fixtures import BOT, locked, with_services

    portal = {**BOT, "components": ["platform", "mcp"]}
    worker = {"command": ["run"], "health": {"command": ["true"]}}
    with_services(
        project,
        {
            "bot": {"hostname": "bot", "routes": {"webhook": {"access": "public"}}},
            "portal": {
                "hostname": "portal",
                "host": "mcp",
                "routes": {"admin": {"access": "public"}},
            },
            "worker": {},
        },
        declared={"bot": BOT, "portal": portal, "worker": worker},
    )
    locked(project)
    if outputs is None:
        outputs = json.loads(json.dumps(STANDARD_OUTPUTS))
        outputs["hosts"]["platform"]["service_hostnames"] = ["bot.data.acme.com"]
    asked = []

    def fetch(url):
        asked.append(url)
        return answers.get(url)

    monkeypatch.setattr(shipping, "fetch_response", fetch)
    monkeypatch.setattr(shipping, "SERVICE_WINDOW", 0)
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
        s3.put_object(Bucket=STATE_BUCKET, Key="prod/outputs.json", Body=json.dumps(outputs))
        real = clients.client
        monkeypatch.setattr(
            clients, "client", lambda svc, region: ssm if svc == "ssm" else real(svc, region)
        )
        monkeypatch.setattr(ship_mod, "poll", lambda url, timeout, **kw: True)
        json_run = invoke("ship", "--env", "prod", "--json")
        text_run = invoke("ship", "--env", "prod")
    return json_run, text_run, asked, ssm


def test_an_unhealthy_service_is_reported_and_the_bundle_stays(invoke, project, monkeypatch):
    health = "https://bot.data.acme.com/_vecten/health"
    r, text, asked, ssm = _ship_with_services(invoke, project, monkeypatch, {health: (500, "")})
    assert r.exit_code == 0, r.stdout
    data = doc(r)
    assert data["hosts"] == ["mcp", "platform"]
    assert data["services"] == {
        "acme.bot": "unhealthy",
        "acme.portal": "waiting for deploy",
        "acme.worker": "not routed",
    }
    assert data["waiting_routes"] == ["acme.portal.admin"]
    assert asked[:1] == [health]
    assert not any("rollback" in " ".join(map(str, sent)) for sent in ssm.sent)
    assert text.exit_code == 0, text.stdout
    assert "service acme.bot: unhealthy" in text.stdout
    assert "routes waiting for `vecten deploy --env prod`: acme.portal.admin" in text.stdout


def test_a_healthy_service_is_reported_healthy(invoke, project, monkeypatch):
    health = "https://bot.data.acme.com/_vecten/health"
    r, _, _, _ = _ship_with_services(invoke, project, monkeypatch, {health: (200, "ok")})
    assert r.exit_code == 0, r.stdout
    assert doc(r)["services"]["acme.bot"] == "healthy"


def test_a_service_that_does_not_answer_is_unhealthy_and_fails_nothing(
    invoke, project, monkeypatch
):
    r, _, _, _ = _ship_with_services(invoke, project, monkeypatch, {})
    assert r.exit_code == 0, r.stdout
    assert doc(r)["services"]["acme.bot"] == "unhealthy"


def test_a_ship_without_services_reports_none(invoke, project, monkeypatch):
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
    assert "services" not in doc(r) and "waiting_routes" not in doc(r)


# The commit status every ship inside Actions records.

RUN_URL = "https://github.com/acme/data/actions/runs/77"


def _ship_in_actions(invoke, project, monkeypatch, ssm, *extra):
    aws_credentials(monkeypatch, project)
    set_facts(project, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/data")
    monkeypatch.setenv("GITHUB_RUN_ID", "77")
    git = FakeGit(answers={("rev-parse", "--verify"): (0, COMMIT + "\n")})
    monkeypatch.setattr(gh_mod, "get_git", lambda: git)
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
        return invoke("ship", "--env", "prod", *extra, "--json")


def test_a_ship_in_actions_sets_pending_then_success_on_its_commit(
    invoke, project, monkeypatch, status_gh
):
    r = _ship_in_actions(invoke, project, monkeypatch, FakeSsm())
    assert r.exit_code == 0, r.stdout
    assert status_gh.posted_statuses() == [
        (COMMIT, "vecten/ship/prod", "pending"),
        (COMMIT, "vecten/ship/prod", "success"),
    ]
    posts = status_gh.named("api", "-X", "POST")
    assert all(f"target_url={RUN_URL}" in call for call in posts)


def test_a_ship_that_rolls_back_sets_failure_on_its_commit(invoke, project, monkeypatch, status_gh):
    ssm = FakeSsm(results={("i-0mcp", "ship"): ("Failed", "health poll failed")})
    r = _ship_in_actions(invoke, project, monkeypatch, ssm)
    assert r.exit_code == 5, r.stdout
    assert status_gh.posted_statuses() == [
        (COMMIT, "vecten/ship/prod", "pending"),
        (COMMIT, "vecten/ship/prod", "failure"),
    ]


def test_the_last_environments_status_is_best_effort(invoke, project, monkeypatch, status_gh):
    # Nothing promotes from prod, so a GitHub outage does not hold its ship back.
    status_gh.failing = ("api",)
    ssm = FakeSsm()
    r = _ship_in_actions(invoke, project, monkeypatch, ssm)
    assert r.exit_code == 0, r.stdout
    assert ssm.kinds() == [("i-0mcp", "ship"), ("i-0platform", "ship")]
    warnings = doc(r)["warnings"]
    assert any("vecten/ship/prod" in w and "pending" in w for w in warnings), warnings
    assert any("success" in w for w in warnings), warnings


def test_a_transient_status_error_is_retried(invoke, project, monkeypatch, status_gh):
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    status_gh.transient["statuses"] = 2
    r = _ship_in_actions(invoke, project, monkeypatch, FakeSsm())
    assert r.exit_code == 0, r.stdout
    assert "warnings" not in doc(r)
    assert status_gh.posted_statuses().count((COMMIT, "vecten/ship/prod", "pending")) == 3
    assert status_gh.latest_status(COMMIT, "vecten/ship/prod") == "success"


def test_the_status_retries_are_bounded(invoke, project, monkeypatch, status_gh):
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    status_gh.transient["statuses"] = 100
    r = _ship_in_actions(invoke, project, monkeypatch, FakeSsm())
    assert r.exit_code == 0, r.stdout
    posts = status_gh.posted_statuses()
    assert len(posts) == 2 * github_env.STATUS_ATTEMPTS


def test_a_push_ship_needs_no_gh_login(invoke, project, monkeypatch, status_gh):
    # Inside Actions GH_TOKEN authenticates gh; `gh auth status` is a laptop check.
    status_gh.logged_in = False
    r = _ship_in_actions(invoke, project, monkeypatch, FakeSsm(), "--commit", COMMIT)
    assert r.exit_code == 0, r.stdout
    assert status_gh.named("auth") == []
    assert status_gh.latest_status(COMMIT, "vecten/ship/prod") == "success"


def test_check_with_commit_is_usage(invoke, no_cloud):
    r = invoke("ship", "--env", "prod", "--check", "--commit", COMMIT, "--json")
    assert r.exit_code == 2, r.stdout
    assert "--commit" in doc(r)["error"]


def test_a_failure_status_that_cannot_be_set_is_named_beside_the_ship_failure(
    invoke, project, monkeypatch, status_gh
):
    real_run = status_gh.run

    def run(args, cwd, input=None):
        if "state=failure" in args:
            return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 502)")
        return real_run(args, cwd, input)

    status_gh.run = run
    ssm = FakeSsm(results={("i-0mcp", "ship"): ("Failed", "health poll failed")})
    r = _ship_in_actions(invoke, project, monkeypatch, ssm)
    assert r.exit_code == 5, r.stdout
    error = doc(r)["error"]
    assert "host mcp" in error and "stays pending" in error


def test_a_ship_from_the_laptop_sets_no_status(invoke, no_cloud, monkeypatch):
    set_facts(no_cloud, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    gh = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    assert invoke("ship", "--env", "prod", "--json").exit_code == 0
    assert gh.posted_statuses() == []


def test_a_ship_skipped_for_a_pending_identity_sets_no_status(
    invoke, snowflake_deployed, monkeypatch, status_gh
):
    aws_credentials(monkeypatch, snowflake_deployed)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    monkeypatch.setattr(clients, "client", lambda svc, region: FakeSsm())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert status_gh.posted_statuses() == []


# Volumes of services a host does not run, as each host's agent names them.


def test_an_aws_ship_reports_the_volumes_each_agent_named(invoke, project, monkeypatch):
    aws_credentials(monkeypatch, project)
    set_facts(project, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    stderr = (
        "vecten agent volume: acme-prod_svc-acme-old-data\n"
        "vecten agent volume: bad name; rm -rf /\n"
    )
    ssm = FakeSsm(results={("i-0platform", "ship"): ("Success", stderr)})
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
        text = invoke("ship", "--env", "prod")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["orphaned_volumes"] == {"platform": ["acme-prod_svc-acme-old-data"]}
    assert (
        "volumes of services no longer bound on platform: acme-prod_svc-acme-old-data"
        in text.stdout
    )


def test_a_scaleway_ship_reports_the_volumes_its_watcher_named(invoke, scaleway):
    scaleway.watcher.volumes[("platform", COMMIT)] = ["acme-prod_svc-acme-old-data", 7]
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["orphaned_volumes"] == {"platform": ["acme-prod_svc-acme-old-data"]}


def test_a_ship_whose_agents_name_no_volume_reports_none(invoke, scaleway):
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert "orphaned_volumes" not in doc(r)
