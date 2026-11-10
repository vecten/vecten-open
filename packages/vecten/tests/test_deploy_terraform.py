import subprocess

import pytest

from vecten.deploy import terraform as terraform_mod
from vecten.deploy.terraform import (
    LOCK_RETRIES,
    SubprocessTerraform,
    get_terraform,
    redacted_tail,
    require_terraform,
    state_key,
    terraform_apply,
    terraform_destroy,
    terraform_init,
    terraform_outputs,
    terraform_plan,
)
from vecten_shared.output import ExitCode, Failure


class Missing:
    def which(self):
        return None

    def run(self, args, cwd, env=None):
        raise AssertionError("a missing terraform ran")


class Present(Missing):
    def which(self):
        return "/usr/local/bin/terraform"


def test_runner_is_injectable(monkeypatch):
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:Present")
    assert isinstance(get_terraform(), Present)


def test_default_runner_is_the_terraform_binary(monkeypatch):
    monkeypatch.delenv("VECTEN_TERRAFORM_RUNNER", raising=False)
    runner = get_terraform()
    assert isinstance(runner, SubprocessTerraform)
    assert runner.binary == "terraform"


def test_missing_terraform_is_a_prerequisite():
    with pytest.raises(Failure) as caught:
        require_terraform(Missing())
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "terraform" in caught.value.message


def test_present_terraform_names_its_path():
    assert require_terraform(Present()) == "/usr/local/bin/terraform"


# The two-step helpers


class Scripted(Present):
    """Records `(args, cwd, env)`; `answers` maps a subcommand to `(exit code, stdout, stderr)`."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def run(self, args, cwd, env=None):
        self.calls.append((list(args), cwd, env))
        code, stdout, stderr = self.answers.get(args[0], (0, "", ""))
        return subprocess.CompletedProcess(args, code, stdout, stderr)


class Sequenced(Present):
    """Answers each call of a subcommand with the next `(exit code, stdout, stderr)` in its list,
    and the last one once the list runs out."""

    def __init__(self, answers):
        self.answers = {command: list(seq) for command, seq in answers.items()}
        self.calls = []

    def run(self, args, cwd, env=None):
        self.calls.append((list(args), cwd, env))
        seq = self.answers.get(args[0], [(0, "", "")])
        code, stdout, stderr = seq.pop(0) if len(seq) > 1 else seq[0]
        return subprocess.CompletedProcess(args, code, stdout, stderr)


# What Terraform 1.16's S3 backend prints, wrapped as it wraps it, when its conditional lock
# write meets a lock object that is gone again by the time it reads the holder's details.
LOCK_VANISHED = """
Error: Error acquiring the state lock

Error message: operation error S3: PutObject, https response error
StatusCode: 412, RequestID: tx1, HostID:
tx1, api error PreconditionFailed: At least
one of the pre-conditions you specified did not hold
unable to retrieve file from S3 bucket
'vecten-acme-prod-state-1' with key
'prod/scaleway.tfstate.tflock': operation error S3: GetObject, https response
error StatusCode: 404, RequestID: tx2, HostID:
tx2, NoSuchKey: The specified key does not
exist.
"""
LOCK_HELD = """
Error: Error acquiring the state lock

Error message: operation error S3: PutObject, https response error
StatusCode: 412, api error PreconditionFailed
Lock Info:
  ID:        0a1b2c3d
  Path:      vecten-acme-prod-state-1/prod/scaleway.tfstate
  Operation: OperationTypePlan
  Who:       runner@fv-az123
"""
LOCK_UNREADABLE = LOCK_VANISHED.replace("StatusCode: 404", "StatusCode: 403").replace(
    "NoSuchKey: The specified key does not\nexist.", "AccessDenied: Access Denied"
)


@pytest.fixture
def slept(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr(terraform_mod, "sleep", delays.append)
    return delays


def test_a_lock_released_while_terraform_read_it_is_taken_again(tmp_path, slept):
    runner = Sequenced({"apply": [(1, "", LOCK_VANISHED), (0, "applied", "")]})
    terraform_apply(runner, tmp_path, tmp_path / "p")
    assert [args[0] for args, _, _ in runner.calls] == ["apply", "apply"]
    assert len(slept) == 1


def test_a_lock_still_held_after_the_timeout_is_not_retried(tmp_path, slept):
    runner = Sequenced({"plan": [(1, "", LOCK_HELD)]})
    with pytest.raises(Failure) as caught:
        terraform_plan(runner, tmp_path, "v.tfvars", tmp_path / "p")
    assert "Error acquiring the state lock" in caught.value.message
    assert len(runner.calls) == 1 and slept == []


def test_a_lock_object_the_key_may_not_read_is_not_retried(tmp_path, slept):
    runner = Sequenced({"destroy": [(1, "", LOCK_UNREADABLE)]})
    with pytest.raises(Failure):
        terraform_destroy(runner, tmp_path, "v.tfvars")
    assert len(runner.calls) == 1 and slept == []


def test_lock_retries_stop_and_report_the_last_error(tmp_path, slept):
    runner = Sequenced({"apply": [(1, "", LOCK_VANISHED)]})
    with pytest.raises(Failure) as caught:
        terraform_apply(runner, tmp_path, tmp_path / "p")
    assert len(runner.calls) == 1 + LOCK_RETRIES
    assert "Error acquiring the state lock" in caught.value.message


def test_state_keys_sit_under_the_environment():
    # The plan role may write lock objects only under `<env>/`, and the lock is `<key>.tflock`.
    assert state_key("prod", "aws") == "prod/aws.tfstate"
    assert state_key("staging", "snowflake") == "staging/snowflake.tfstate"


def test_init_configures_the_s3_backend_with_a_lockfile(tmp_path):
    runner = Scripted()
    terraform_init(
        runner,
        tmp_path,
        bucket="vecten-acme-prod-state",
        key="prod/aws.tfstate",
        region="eu-central-1",
    )
    [(args, cwd, env)] = runner.calls
    assert cwd == tmp_path
    assert args[0] == "init"
    assert "-input=false" in args and "-reconfigure" in args
    assert "-backend-config=bucket=vecten-acme-prod-state" in args
    assert "-backend-config=key=prod/aws.tfstate" in args
    assert "-backend-config=region=eu-central-1" in args
    assert "-backend-config=use_lockfile=true" in args
    assert "-backend-config=encrypt=true" in args
    assert env is None


def test_plan_saves_a_plan_file_and_returns_its_text(tmp_path):
    out = tmp_path / "prod.tfplan"
    runner = Scripted({"show": (0, "Plan: 3 to add, 0 to change, 0 to destroy.\n", "")})
    text = terraform_plan(
        runner, tmp_path, "../../envs/prod.tfvars", out, extra_vars=(("destroying", "true"),)
    )
    assert text == "Plan: 3 to add, 0 to change, 0 to destroy.\n"
    (plan, _, _), (show, _, _) = runner.calls
    assert plan[0] == "plan"
    assert "-input=false" in plan
    assert f"-out={out}" in plan
    assert "-var-file=../../envs/prod.tfvars" in plan
    assert plan[plan.index("-var") + 1] == "destroying=true"
    assert show == ["show", "-no-color", str(out)]


def test_apply_applies_the_saved_plan(tmp_path):
    out = tmp_path / "prod.tfplan"
    runner = Scripted()
    terraform_apply(runner, tmp_path, out)
    [(args, cwd, _)] = runner.calls
    assert args[0] == "apply"
    assert "-input=false" in args
    assert args[-1] == str(out)
    assert not any(a.startswith("-var") for a in args)


def test_state_locking_commands_wait_for_the_lock(tmp_path):
    runner = Scripted()
    terraform_plan(runner, tmp_path, "v.tfvars", tmp_path / "p")
    terraform_apply(runner, tmp_path, tmp_path / "p")
    terraform_destroy(runner, tmp_path, "v.tfvars")
    by_command = {args[0]: args for args, _, _ in runner.calls}
    for command in ("plan", "apply", "destroy"):
        assert "-lock-timeout=10m" in by_command[command], command


def test_helpers_pass_the_extra_environment(tmp_path):
    runner = Scripted({"output": (0, "{}", "")})
    extra = {"SNOWFLAKE_USER": "deployer"}
    terraform_init(
        runner, tmp_path, bucket="b", key="prod/snowflake.tfstate", region="r", env=extra
    )
    terraform_plan(runner, tmp_path, "v.tfvars", tmp_path / "p", env=extra)
    terraform_apply(runner, tmp_path, tmp_path / "p", env=extra)
    terraform_outputs(runner, tmp_path, env=extra)
    assert [env for _, _, env in runner.calls] == [extra] * 5


def test_outputs_are_read_as_json(tmp_path):
    body = '{"region": {"value": "eu-central-1", "type": "string", "sensitive": false}}'
    runner = Scripted({"output": (0, body, "")})
    assert terraform_outputs(runner, tmp_path) == {
        "region": {"value": "eu-central-1", "type": "string", "sensitive": False}
    }
    assert runner.calls[0][0] == ["output", "-json"]


def test_outputs_that_are_not_json_are_a_tool_failure(tmp_path):
    with pytest.raises(Failure) as caught:
        terraform_outputs(Scripted({"output": (0, "not json", "")}), tmp_path)
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "output" in caught.value.message


def test_a_failed_step_names_the_step_and_exit_code_and_drops_secret_lines(tmp_path):
    noise = [f"line {n}" for n in range(30)]
    stderr = "\n".join(
        [
            *noise,
            'Error: invalid value password = "hunter2"',
            "  client_secret: s3cr3t",
            "  GITHUB_TOKEN ghp_abc",
            "  access_key=AKIAEXAMPLE",
            "Error: creating EC2 instance: UnauthorizedOperation",
        ]
    )
    runner = Scripted({"plan": (1, "plan stdout", stderr)})
    with pytest.raises(Failure) as caught:
        terraform_plan(runner, tmp_path, "v.tfvars", tmp_path / "p")
    message = caught.value.message
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "terraform plan" in message and "exit 1" in message
    assert "UnauthorizedOperation" in message
    for leaked in ("hunter2", "s3cr3t", "ghp_abc", "AKIAEXAMPLE", "plan stdout"):
        assert leaked not in message
    kept = message.splitlines()[1:]
    assert len(kept) == 20
    assert "line 10" not in kept and "line 11" in kept and "line 29" in kept
    assert len(runner.calls) == 1


def test_redacted_tail_drops_url_credentials():
    text = "fatal: unable to access 'https://x-access:abc123@github.com/acme/data/'\nok line"
    assert redacted_tail(text) == ["ok line"]


PEM_LINE = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDdlatRjRjogo3W"
PRIVATE_KEY = f"-----BEGIN PRIVATE KEY-----\n{PEM_LINE}\nAAAA\n-----END PRIVATE KEY-----\n"


def test_inherited_snowflake_variables_never_reach_terraform(tmp_path, monkeypatch):
    seen = {}

    def run(argv, cwd, env, capture_output, text):
        seen.update(env)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("SNOWFLAKE_ROLE", "ACCOUNTADMIN")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2")
    monkeypatch.setenv("PATH", "/usr/bin")
    SubprocessTerraform().run(["plan"], tmp_path, {"SNOWFLAKE_USER": "VECTEN_DEPLOY"})
    assert "SNOWFLAKE_ROLE" not in seen and "SNOWFLAKE_PASSWORD" not in seen
    assert seen["SNOWFLAKE_USER"] == "VECTEN_DEPLOY" and seen["PATH"] == "/usr/bin"
    seen.clear()
    SubprocessTerraform().run(["plan"], tmp_path)
    assert not any(name.startswith("SNOWFLAKE_") for name in seen)
    assert seen["PATH"] == "/usr/bin"


def test_only_the_values_named_secret_are_masked(tmp_path):
    env = {
        "SNOWFLAKE_ORGANIZATION_NAME": "ACME",
        "SNOWFLAKE_ACCOUNT_NAME": "PROD",
        "SNOWFLAKE_AUTHENTICATOR": "SNOWFLAKE_JWT",
        "SNOWFLAKE_PRIVATE_KEY": PRIVATE_KEY,
    }
    stderr = f"Error: account ACME-PROD refused SNOWFLAKE_JWT sign-in\n  {PEM_LINE}\n"
    runner = Scripted({"plan": (1, "", stderr)})
    with pytest.raises(Failure) as caught:
        terraform_plan(runner, tmp_path, "v.tfvars", tmp_path / "p", env=env, secrets=[PRIVATE_KEY])
    message = caught.value.message
    assert "account ACME-PROD refused SNOWFLAKE_JWT sign-in" in message
    assert PEM_LINE[:16] not in message


def test_a_failed_step_redacts_the_secrets_it_was_given(tmp_path):
    secrets = ["VECTEN_DEPLOY_USER", PRIVATE_KEY]
    env = {"SNOWFLAKE_USER": "VECTEN_DEPLOY_USER", "SNOWFLAKE_PRIVATE_KEY": PRIVATE_KEY}
    stderr = "\n".join(
        [
            "Error: user VECTEN_DEPLOY_USER could not authenticate",
            f"  {PEM_LINE}",
            f"  decoding {PEM_LINE[10:30]} failed",
            "Error: 390144 (08004): JWT is invalid",
            "Error: account not reachable",
        ]
    )
    runner = Scripted({"plan": (1, "", stderr)})
    with pytest.raises(Failure) as caught:
        terraform_plan(runner, tmp_path, "v.tfvars", tmp_path / "p", env=env, secrets=secrets)
    message = caught.value.message
    assert "VECTEN_DEPLOY_USER" not in message
    assert PEM_LINE[10:26] not in message
    assert "account not reachable" in message
    assert "user *** could not authenticate" in message


def test_redacted_tail_drops_any_sixteen_character_fragment_of_a_value():
    value = "abcdefghijklmnopqrstuvwxyz0123456789"
    text = f"one {value[5:21]} two\nshort {value[5:20]} kept\nclean line"
    assert redacted_tail(text, secrets=[value]) == [f"short {value[5:20]} kept", "clean line"]


def test_a_value_shorter_than_sixteen_characters_is_masked_where_it_appears():
    assert redacted_tail("user ACME_DEPLOY refused", secrets=["ACME_DEPLOY"]) == [
        "user *** refused"
    ]


@pytest.mark.parametrize(
    "marker",
    [
        "ExpiredToken",
        "InvalidClientTokenId",
        "NoCredentialProviders",
        "No valid credential sources found",
    ],
)
def test_a_failed_init_names_the_aws_login(tmp_path, marker):
    stderr = f"Error: configuring S3 Backend: {marker}: the security token is expired"
    runner = Scripted({"init": (1, "", stderr)})
    with pytest.raises(Failure) as caught:
        terraform_init(runner, tmp_path, bucket="b", key="prod/aws.tfstate", region="eu-west-1")
    message = caught.value.message
    assert "AWS login" in message
    assert marker.removesuffix(" found") in message
