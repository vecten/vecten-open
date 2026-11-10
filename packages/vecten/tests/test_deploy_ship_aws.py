import json
import shutil
import subprocess

import boto3
import pytest
from moto import mock_aws

from vecten.deploy import run as run_mod
from vecten.deploy.aws import AwsProvider, clients
from vecten.deploy.aws import ship as ship_mod
from vecten.github import gh as gh_mod
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs

from .deploy_fixtures import (
    BUCKET,
    COMMIT,
    REGION,
    STANDARD_OUTPUTS,
    STATE_BUCKET,
    FakeGit,
    FakeSsm,
    aws_credentials,
    minimal_outputs,
    render_aws,
    set_facts,
    write_outputs,
)

AIRFLOW_URL = "https://airflow.data.acme.com/api/v2/monitor/health"
MCP_URL = "https://mcp.data.acme.com/health"


class Polls:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.calls = []
        self.predicates = {}

    def __call__(self, url, timeout, interval=5, **kwargs):
        self.calls.append((url, timeout))
        self.predicates[url] = kwargs.get("healthy")
        return url not in self.failing


def s3():
    return boto3.client("s3", region_name=REGION)


def make_bucket(name):
    s3().create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": REGION})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A deployed standard project, its outputs in the state bucket, SSM and polls faked."""
    aws_credentials(monkeypatch, tmp_path)
    root = render_aws(tmp_path / "proj")
    set_facts(root, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)
    with mock_aws():
        make_bucket(BUCKET)
        make_bucket(STATE_BUCKET)
        s3().put_object(
            Bucket=STATE_BUCKET, Key="prod/outputs.json", Body=json.dumps(STANDARD_OUTPUTS)
        )
        ssm = FakeSsm()
        real = clients.client

        def client(service, region):
            assert region == REGION
            return ssm if service == "ssm" else real(service, region)

        monkeypatch.setattr(clients, "client", client)
        polls = Polls()
        monkeypatch.setattr(ship_mod, "poll", polls)
        monkeypatch.setattr(ship_mod, "sleep", lambda seconds: None)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        yield type("Env", (), {"root": root, "ssm": ssm, "polls": polls})


def ship(env, git=None):
    return run_mod.ship_env(env.root, "prod", gh=None, git=git or FakeGit(), check=False)


def test_order_ships_the_setup_host_first():
    standard = EnvironmentOutputs.model_validate(STANDARD_OUTPUTS)
    assert ship_mod.ship_order(standard) == ["mcp", "platform"]
    assert ship_mod.ship_order(EnvironmentOutputs.model_validate(minimal_outputs())) == ["main"]
    platform_only = json.loads(json.dumps(STANDARD_OUTPUTS))
    del platform_only["hosts"]["mcp"]
    platform_only["setup_host"] = "platform"
    assert ship_mod.ship_order(EnvironmentOutputs.model_validate(platform_only)) == ["platform"]


def test_ship_in_actions_reaches_every_host_setup_host_first(env):
    result = ship(env)
    assert env.ssm.kinds() == [("i-0mcp", "ship"), ("i-0platform", "ship")]
    assert result["hosts"] == ["mcp", "platform"]
    assert result["commit"] == COMMIT
    assert result["dispatched"] is False
    for host in ("mcp", "platform"):
        s3().head_object(Bucket=BUCKET, Key=f"bundles/{host}/{COMMIT}.tar.gz")


def test_outputs_come_from_the_state_bucket(env):
    local = env.root / ".vecten" / "environments" / "prod.json"
    assert not local.exists()
    ship(env)
    assert EnvironmentOutputs.model_validate_json(local.read_text()) == (
        EnvironmentOutputs.model_validate(STANDARD_OUTPUTS)
    )


def test_ssm_command_names_the_release_and_holds_no_secret(env):
    (env.root / ".env").write_text("HUBSPOT_API_KEY=hunter2\n")
    ship(env)
    sent = env.ssm.sent[0]
    assert sent["DocumentName"] == "AWS-RunShellScript"
    assert sent["TimeoutSeconds"] == 1800
    assert sent["InstanceIds"] == ["i-0mcp"]
    script = "\n".join(sent["Parameters"]["commands"])
    release = f"/opt/vecten/releases/{COMMIT}"
    assert f"s3://{BUCKET}/bundles/mcp/{COMMIT}.tar.gz" in script
    assert f"/opt/vecten/incoming/{COMMIT}.tar.gz" in script
    assert f"python3 {release}/.vecten/ship/apply.py ship --release {release}" in script
    assert "hunter2" not in json.dumps(env.ssm.sent)
    assert "PASSWORD" not in json.dumps(env.ssm.sent)


def test_success_polls_every_component_url_with_the_first_ship_window(env):
    ship(env)
    assert env.polls.calls == [(MCP_URL, 600), (AIRFLOW_URL, 600)]


def test_each_component_is_judged_by_its_own_health_answer(env):
    ship(env)
    assert env.polls.predicates == {
        AIRFLOW_URL: ship_mod.airflow_healthy,
        MCP_URL: ship_mod.answers_200,
    }


def test_a_host_with_an_earlier_bundle_gets_the_shorter_window(env):
    s3().put_object(Bucket=BUCKET, Key="bundles/platform/feedface.tar.gz", Body=b"x")
    ship(env)
    assert env.polls.calls == [(MCP_URL, 600), (AIRFLOW_URL, 300)]


def test_failed_poll_restores_previous(env):
    env.polls.failing.add(AIRFLOW_URL)
    with pytest.raises(Failure) as exc:
        ship(env)
    assert exc.value.code == ExitCode.TOOL_FAILED
    assert "platform" in exc.value.message
    assert AIRFLOW_URL in exc.value.message
    assert env.ssm.kinds() == [
        ("i-0mcp", "ship"),
        ("i-0platform", "ship"),
        ("i-0platform", "rollback"),
    ]
    script = "\n".join(env.ssm.sent[-1]["Parameters"]["commands"])
    assert f"python3 /opt/vecten/releases/{COMMIT}/.vecten/ship/apply.py rollback" in script
    assert exc.value.details["component"] == "platform"
    assert exc.value.details["rolled_back"] is True


def test_failed_rollback_is_named_too(env):
    env.polls.failing.add(MCP_URL)
    env.ssm.results[("i-0mcp", "rollback")] = (
        "Failed",
        "vecten agent rollback: no previous bundle\n",
    )
    with pytest.raises(Failure) as exc:
        ship(env)
    assert "mcp" in exc.value.message
    assert "no previous bundle" in exc.value.message
    assert exc.value.details["rolled_back"] is False
    assert env.ssm.kinds() == [("i-0mcp", "ship"), ("i-0mcp", "rollback")]


def test_failed_invocation_output_is_not_echoed(env):
    env.ssm.results[("i-0mcp", "ship")] = (
        "Failed",
        "Traceback (most recent call last):\n"
        "  PASSWORD=hunter2\n"
        "vecten agent ship: setup SQL: psql failed on instance state (exit 2)\n"
        "AWS_SECRET_ACCESS_KEY=hunter3\n",
    )
    with pytest.raises(Failure) as exc:
        ship(env)
    assert exc.value.code == ExitCode.TOOL_FAILED
    message = exc.value.message
    assert "mcp" in message
    assert "vecten agent ship: setup SQL: psql failed on instance state (exit 2)" in message
    assert "hunter2" not in message and "hunter3" not in message
    assert "hunter" not in json.dumps(exc.value.details)
    # The agent failed before or after its swap; it keeps `current` itself, so no rollback.
    assert env.ssm.kinds() == [("i-0mcp", "ship")]


@pytest.mark.parametrize(
    ("outcome", "rolled_back"),
    [(f"rolled back to {'a' * 40}", True), ("no previous release to roll back to", False)],
)
def test_agent_rollback_after_its_swap_is_reported_without_a_second_rollback(
    env, outcome, rolled_back
):
    line = f"vecten agent ship: compose up: docker compose up failed (exit 1); {outcome}"
    env.ssm.results[("i-0mcp", "ship")] = ("Failed", f"{line}\n")
    with pytest.raises(Failure) as exc:
        ship(env)
    assert exc.value.message.endswith(line)
    assert exc.value.details["rolled_back"] is rolled_back
    assert env.ssm.kinds() == [("i-0mcp", "ship")]
    assert env.polls.calls == []


def test_a_failed_agent_run_prints_the_container_log(env, monkeypatch):
    monkeypatch.setattr(ship_mod, "_fetched", {"fetched-value-123"})
    line = "vecten agent ship: compose up: docker compose up failed (exit 1); rolled back to c1"
    env.ssm.results[("i-0mcp", "ship")] = (
        "Failed",
        "vecten agent log: mcp-server: connecting to postgres://mcp:hunter22@db/state\n"
        "noise PASSWORD=hunter2\n"
        "vecten agent log: mcp-server: token fetched-value-123 refused\n"
        f"{line}\n",
    )
    with pytest.raises(Failure) as exc:
        ship(env)
    expected = [
        "mcp-server: connecting to postgres://[redacted]@db/state",
        "mcp-server: token [redacted] refused",
    ]
    assert exc.value.details["container_log"] == expected
    assert exc.value.message == (
        f"shipping to host mcp failed (Failed): {line}\n"
        "last output of compose and the failing containers:\n  " + "\n  ".join(expected)
    )
    assert "hunter2" not in exc.value.message and "fetched-value" not in exc.value.message


def test_a_rolled_back_poll_prints_the_container_log(env):
    env.polls.failing.add(AIRFLOW_URL)
    env.ssm.results[("i-0platform", "rollback")] = (
        "Success",
        "vecten agent log: airflow-api-server: database is locked\n",
    )
    with pytest.raises(Failure) as exc:
        ship(env)
    assert exc.value.details["rolled_back"] is True
    assert exc.value.details["container_log"] == ["airflow-api-server: database is locked"]
    assert exc.value.message.endswith(
        "host platform is back on its previous bundle\n"
        "last output of compose and the failing containers:\n"
        "  airflow-api-server: database is locked"
    )


def test_a_failure_without_log_lines_adds_nothing(env):
    env.ssm.results[("i-0mcp", "ship")] = ("Failed", "vecten agent ship: swap: failed\n")
    with pytest.raises(Failure) as exc:
        ship(env)
    assert (
        exc.value.message == "shipping to host mcp failed (Failed): vecten agent ship: swap: failed"
    )
    assert exc.value.details["container_log"] == []


def test_invocation_without_an_agent_line_names_host_and_status_only(env):
    env.ssm.results[("i-0mcp", "ship")] = ("TimedOut", "LOADER_PASSWORD=hunter2\n")
    with pytest.raises(Failure) as exc:
        ship(env)
    assert "mcp" in exc.value.message and "TimedOut" in exc.value.message
    assert "hunter2" not in exc.value.message


def test_agent_line_is_truncated_to_300_characters():
    line = "vecten agent ship: certificates: " + "x" * 1000
    found = ship_mod.agent_line(f"noise\n{line}\n")
    assert found is not None and len(found) == 300 and found.startswith("vecten agent ship")


def test_agent_line_redacts_values_this_process_fetched():
    found = ship_mod.agent_line("vecten agent ship: release: s3cr3t-value\n", {"s3cr3t-value"})
    assert found == "vecten agent ship: release: ***"


def test_agent_line_ignores_lines_the_agent_did_not_print():
    assert ship_mod.agent_line("PASSWORD=hunter2\n") is None
    assert ship_mod.agent_line("") is None


def test_a_pending_invocation_is_waited_for(env, monkeypatch):
    env.ssm.pending = 2
    waits = []
    monkeypatch.setattr(ship_mod, "sleep", waits.append)
    ship(env)
    assert len(waits) == 4


def test_ship_before_deploy_is_refused(env):
    set_facts(env.root, deployed_commit=None)
    with pytest.raises(Failure) as exc:
        ship(env)
    assert exc.value.code == ExitCode.REFUSED
    assert "vecten deploy --env prod" in exc.value.message
    assert env.ssm.sent == []


def test_head_that_is_not_a_commit_is_refused(env):
    with pytest.raises(Failure) as exc:
        ship(env, git=FakeGit(head="not-a-sha"))
    assert exc.value.code == ExitCode.TOOL_FAILED
    assert env.ssm.sent == []


def test_provider_ship_sends_only_the_host_carrying_the_component(env, monkeypatch):
    write_outputs(env.root, STANDARD_OUTPUTS)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    AwsProvider(env.root, "prod").ship("prod", "platform", env.root)
    assert env.ssm.kinds() == [("i-0platform", "ship")]
    assert env.polls.calls == [(AIRFLOW_URL, 600)]


def test_provider_ship_refuses_a_component_without_a_host(env, monkeypatch):
    outputs = json.loads(json.dumps(STANDARD_OUTPUTS))
    del outputs["hosts"]["mcp"]
    outputs["setup_host"] = "platform"
    write_outputs(env.root, outputs)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    with pytest.raises(Failure) as exc:
        AwsProvider(env.root, "prod").ship("prod", "mcp", env.root)
    assert exc.value.code == ExitCode.PREREQUISITE
    assert env.ssm.sent == []


def test_ssm_script_waits_for_user_data_first(env):
    ship(env)
    commands = env.ssm.sent[0]["Parameters"]["commands"]
    assert commands[0] == ship_mod.WAIT_FOR_USER_DATA
    assert "cloud-init status --wait" in commands[0]


def run_user_data_wait(tmp_path, cloud_init: str | None):
    """Run the ship script's first line with `cloud_init` as the body of a fake cloud-init."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("sh", "bash", "cat", "echo"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    if cloud_init is not None:
        fake = bin_dir / "cloud-init"
        fake.write_text(f"#!/bin/sh\n{cloud_init}\n")
        fake.chmod(0o755)
    script = f"{ship_mod.WAIT_FOR_USER_DATA}\necho reached\n"
    return subprocess.run(
        [shutil.which("sh"), "-c", script],
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "cloud_init",
    [
        'echo "status: done"; exit 0',
        'echo "status: disabled"; exit 0',
        'echo "status: done"; exit 2',
        None,
    ],
    ids=["done", "disabled", "degraded", "not-installed"],
)
def test_user_data_wait_passes_unless_cloud_init_reports_an_error(tmp_path, cloud_init):
    result = run_user_data_wait(tmp_path, cloud_init)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reached"


def test_user_data_wait_fails_naming_cloud_init_on_an_error(tmp_path):
    result = run_user_data_wait(tmp_path, 'echo "status: error"; exit 1')
    assert result.returncode == 1
    assert "reached" not in result.stdout
    line = ship_mod.agent_line(result.stderr)
    assert line is not None and line.startswith("vecten ship: cloud-init")


def test_a_host_not_yet_registered_with_ssm_is_retried(env, monkeypatch):
    env.ssm.unregistered = 3
    waits = []
    monkeypatch.setattr(ship_mod, "sleep", waits.append)
    ship(env)
    assert waits.count(15) == 3
    assert env.ssm.kinds() == [("i-0mcp", "ship"), ("i-0platform", "ship")]


def test_a_host_that_never_registers_gives_up_after_ten_minutes(env, monkeypatch):
    env.ssm.unregistered = 10_000
    waits = []
    monkeypatch.setattr(ship_mod, "sleep", waits.append)
    with pytest.raises(Failure) as exc:
        ship(env)
    assert exc.value.code == ExitCode.TOOL_FAILED
    assert "i-0mcp" in exc.value.message and "SSM" in exc.value.message
    assert sum(waits) == 600 and env.ssm.attempts == 41


def test_rollback_that_cannot_be_sent_keeps_the_poll_failure(env):
    env.polls.failing.add(AIRFLOW_URL)
    env.ssm.refused.add("rollback")
    with pytest.raises(Failure) as exc:
        ship(env)
    message = exc.value.message
    assert "platform" in message and AIRFLOW_URL in message
    assert "rollback could not be sent: ClientError AccessDeniedException" in message
    assert "LOADER_PASSWORD" not in message
    assert exc.value.details["rolled_back"] is False
    assert exc.value.details["component"] == "platform"


HEALTHY_AIRFLOW = json.dumps(
    {
        "metadatabase": {"status": "healthy"},
        "scheduler": {"status": "healthy", "latest_scheduler_heartbeat": "2026-10-18T10:00:00Z"},
    }
)
UNHEALTHY_AIRFLOW = json.dumps(
    {"metadatabase": {"status": "unhealthy"}, "scheduler": {"status": "healthy"}}
)


def poll_answers(component, answers, timeout=30):
    """Poll `component` against a fixed answer sequence; the last answer repeats."""
    answers = list(answers)
    clock = iter(range(0, 1000, 5))
    seen = []

    def fetch(url):
        seen.append(url)
        return answers[min(len(seen), len(answers)) - 1]

    healthy = ship_mod.poll(
        "https://x/health",
        timeout=timeout,
        healthy=ship_mod.HEALTH[component].healthy,
        fetch=fetch,
        sleep=lambda s: None,
        clock=lambda: next(clock),
    )
    return healthy, seen


@pytest.mark.parametrize(
    "answer",
    [(401, ""), (503, ""), (200, UNHEALTHY_AIRFLOW), (200, "<html>"), (200, "[]"), None],
    ids=["basic-auth-401", "503", "unhealthy-metadatabase", "not-json", "not-an-object", "down"],
)
def test_platform_poll_fails_unless_airflow_says_its_metadatabase_is_healthy(answer):
    healthy, seen = poll_answers("platform", [answer])
    assert not healthy
    assert 3 <= len(seen) <= 8


def test_platform_poll_succeeds_on_a_healthy_metadatabase():
    healthy, seen = poll_answers("platform", [None, (502, ""), (200, HEALTHY_AIRFLOW)])
    assert healthy and len(seen) == 3


@pytest.mark.parametrize("answer", [(401, ""), (404, ""), (503, ""), None])
def test_mcp_poll_needs_200_from_health(answer):
    assert not poll_answers("mcp", [answer])[0]
    assert poll_answers("mcp", [answer, (200, "ok")])[0]


def test_feedback_is_collected_once_before_aws_ships_any_host(env, monkeypatch):
    calls = []

    def metadata(root, config, commit, lineage):
        calls.append(commit)
        assert not env.ssm.kinds()
        return {"commit": commit, "ancestors": [], "resolutions": []}

    monkeypatch.setattr("vecten.deploy.bundle.feedback_manifest", metadata)
    assert ship(env)["hosts"] == ["mcp", "platform"]
    assert calls == [COMMIT]


def test_read_object_reads_the_environment_bucket(env):
    s3().put_object(Bucket=BUCKET, Key="platform/dbt/built_commit", Body=b"abc\n")
    plane = AwsProvider(env.root, "prod")
    plane.stored_outputs()
    assert plane.read_object("platform/dbt/built_commit") == b"abc\n"
    assert plane.read_object("platform/dbt/runs/missing.json") is None
