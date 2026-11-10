"""The Terraform binary as an injectable runner (`VECTEN_TERRAFORM_RUNNER`), and the steps
`deploy` runs with it: init against the state bucket, plan to a saved file, apply that file,
read the outputs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vecten.cli.injection import load_runner
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import VERSIONS

TERRAFORM_RUNNER_VAR = "VECTEN_TERRAFORM_RUNNER"
TAIL_LINES = 20
# A pull request's plan, a push's plan and a deploy share one state lock and can start in the
# same second; waiting for the lock lets each run in turn instead of the later ones failing.
LOCK_TIMEOUT = "-lock-timeout=10m"
LOCK_RETRIES = 3
LOCK_RETRY_DELAY = 5
# Terraform's lock wait gives up at once when the lock object is gone again before it reads
# the holder's details: its conditional write's 412 meets a 404 on the read. That command
# never held the lock and changed nothing, so it runs again.
_LOCK_VANISHED = (
    "Error acquiring the state lock",
    "unable to retrieve file from S3 bucket",
    "StatusCode: 404",
)
# A failing provider can print variable values, request parameters or a remote URL holding
# credentials on stderr, so any line that looks like one never reaches a message.
_SECRET_LINE = re.compile(
    r"password|secret|token|credential|private|key\s*[=:]|://[^\s/@]+@", re.IGNORECASE
)
OWNED_ENV_PREFIXES = ("SNOWFLAKE_",)
FRAGMENT = 16
REDACTED = "***"

sleep = time.sleep
AWS_LOGIN_MARKERS = (
    "ExpiredToken",
    "InvalidClientTokenId",
    "NoCredentialProviders",
    "No valid credential sources",
)


class TerraformRunner(Protocol):
    def which(self) -> str | None: ...
    def run(
        self, args: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess: ...


class SubprocessTerraform:
    binary = "terraform"

    def which(self) -> str | None:
        return shutil.which(self.binary)

    def run(
        self, args: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        # `env` adds to the inherited environment; Terraform needs the cloud credentials and
        # PATH it would otherwise lose. A provider setting under an owned prefix comes only from
        # `env`: an inherited SNOWFLAKE_ROLE or SNOWFLAKE_PASSWORD would sign the deploy user
        # in differently.
        inherited = {k: v for k, v in os.environ.items() if not k.startswith(OWNED_ENV_PREFIXES)}
        merged = {**inherited, **(env or {})}
        return subprocess.run(
            [self.binary, *args], cwd=cwd, env=merged, capture_output=True, text=True
        )


def get_terraform() -> TerraformRunner:
    return load_runner(TERRAFORM_RUNNER_VAR, SubprocessTerraform)


def require_terraform(runner: TerraformRunner) -> str:
    """The binary's path, refusing when Terraform is not installed."""
    path = runner.which()
    if path is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"`terraform` is not installed; install Terraform {VERSIONS.terraform} and retry",
        )
    return path


def state_key(env: str, root: str) -> str:
    """The state object of one root in the environment's state bucket.

    The S3 backend's lock object is `<key>.tflock`, and the plan role may write lock objects
    only under `<env>/`, so every key of an environment sits under that prefix.
    """
    return f"{env}/{root}.tfstate"


def _fragments(value: str) -> set[str]:
    return {value[i : i + FRAGMENT] for i in range(len(value) - FRAGMENT + 1)}


def redacted_tail(stderr: str, lines: int = TAIL_LINES, secrets: Iterable[str] = ()) -> list[str]:
    """At most `lines` last non-blank lines of a tool's error output, without any line that
    may hold a secret.

    Each of `secrets` is masked where it appears whole, and a line holding any `FRAGMENT`
    characters of one is dropped: a provider can quote one line of a multi-line key.
    """
    values = sorted({s for s in secrets if s}, key=len, reverse=True)
    fragments = set().union(*(_fragments(v) for v in values)) if values else set()
    kept = []
    for line in (stderr or "").splitlines():
        for value in values:
            line = line.replace(value, REDACTED)
        if not line.strip() or _SECRET_LINE.search(line):
            continue
        if any(line[i : i + FRAGMENT] in fragments for i in range(len(line) - FRAGMENT + 1)):
            continue
        kept.append(line)
    return kept[-lines:]


def _login_hint(step: str, stderr: str) -> str | None:
    # The keyword filter drops these lines, yet they are the whole diagnosis of a refused
    # login, and each marker is a fixed AWS string that carries no value.
    if step != "init":
        return None
    found = next((m for m in AWS_LOGIN_MARKERS if m in (stderr or "")), None)
    if found is None:
        return None
    return (
        f"the S3 backend refused the AWS login ({found}); renew the AWS login "
        "(inside Actions, the job's OIDC role) and retry"
    )


def _lock_vanished(stderr: str) -> bool:
    # Terraform wraps its messages, so a phrase can span lines.
    flat = " ".join((stderr or "").split())
    return all(phrase in flat for phrase in _LOCK_VANISHED)


def _run(
    runner: TerraformRunner,
    step: str,
    args: list[str],
    cwd: Path,
    env: dict[str, str] | None,
    secrets: Iterable[str] = (),
) -> subprocess.CompletedProcess:
    result = runner.run(args, cwd, env)
    for _ in range(LOCK_RETRIES):
        if result.returncode == 0 or not _lock_vanished(result.stderr):
            break
        sleep(LOCK_RETRY_DELAY)
        result = runner.run(args, cwd, env)
    if result.returncode == 0:
        return result
    # Only the filtered tail of stderr is reported: stdout carries the plan, which can name
    # every resource attribute.
    tail = redacted_tail(result.stderr, secrets=secrets)
    message = f"terraform {step} in {cwd.name} failed (exit {result.returncode})"
    hint = _login_hint(step, result.stderr)
    if hint:
        message += f"; {hint}"
    if tail:
        message += "; the last lines of its error output:\n" + "\n".join(tail)
    raise Failure(
        ExitCode.TOOL_FAILED, message, details={"step": step, "exit_code": result.returncode}
    )


def s3_backend(bucket: str, key: str, region: str) -> dict[str, str]:
    """The S3 backend settings of one root's state, locked by an S3 lock object."""
    return {
        "bucket": bucket,
        "key": key,
        "region": region,
        "use_lockfile": "true",
        "encrypt": "true",
    }


def terraform_init(
    runner: TerraformRunner,
    root_dir: Path,
    *,
    bucket: str,
    key: str,
    region: str,
    env: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> None:
    """Point the root's S3 backend at the environment's state in `bucket`.

    Every helper masks `secrets` in a failure's error output.
    """
    init_backend(runner, root_dir, s3_backend(bucket, key, region), env=env, secrets=secrets)


def init_backend(
    runner: TerraformRunner,
    root_dir: Path,
    backend: Mapping[str, str],
    *,
    env: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> None:
    """Init the root with one `-backend-config` argument per setting in `backend`, in order."""
    args = ["init", "-input=false", "-reconfigure", "-no-color"]
    args += [f"-backend-config={name}={value}" for name, value in backend.items()]
    _run(runner, "init", args, root_dir, env, secrets)


def terraform_plan(
    runner: TerraformRunner,
    root_dir: Path,
    var_file: str,
    out: Path,
    extra_vars: Iterable[tuple[str, str]] = (),
    *,
    env: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
    targets: Iterable[str] = (),
    refresh: bool = True,
) -> str:
    """Plan into the saved file `out` and return the plan as `terraform show` prints it.

    `var_file` is relative to `root_dir`; `extra_vars` are `(name, value)` pairs; `targets`
    are resource addresses the plan is limited to. Without `refresh` the plan compares the
    configuration with the stored state and reads nothing from the cloud.
    """
    args = [
        "plan",
        "-input=false",
        "-no-color",
        LOCK_TIMEOUT,
        f"-out={out}",
        f"-var-file={var_file}",
    ]
    if not refresh:
        args.append("-refresh=false")
    for name, value in extra_vars:
        args += ["-var", f"{name}={value}"]
    args += [f"-target={target}" for target in targets]
    secrets = tuple(secrets)
    _run(runner, "plan", args, root_dir, env, secrets)
    show = ["show", "-no-color", str(out)]
    return _run(runner, "show", show, root_dir, env, secrets).stdout


def terraform_apply(
    runner: TerraformRunner,
    root_dir: Path,
    plan_file: Path,
    *,
    env: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> None:
    """Apply exactly the saved plan; a saved plan applies without a prompt and takes no vars."""
    args = ["apply", "-input=false", "-no-color", LOCK_TIMEOUT, str(plan_file)]
    _run(runner, "apply", args, root_dir, env, secrets)


def terraform_plan_changes(
    runner: TerraformRunner,
    root_dir: Path,
    plan_file: Path,
    *,
    env: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """The saved plan's `resource_changes`, from `terraform show -json`."""
    result = _run(runner, "show", ["show", "-json", str(plan_file)], root_dir, env, secrets)
    try:
        found = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        found = None
    if not isinstance(found, dict):
        raise Failure(
            ExitCode.TOOL_FAILED, f"terraform show -json in {root_dir.name} printed no JSON"
        )
    return found.get("resource_changes") or []


def terraform_state_list(
    runner: TerraformRunner,
    root_dir: Path,
    *,
    env: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> list[str]:
    """The addresses in the root's state; empty when the state holds nothing."""
    result = _run(runner, "state list", ["state", "list"], root_dir, env, secrets)
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def terraform_destroy(
    runner: TerraformRunner,
    root_dir: Path,
    var_file: str,
    extra_vars: Iterable[tuple[str, str]] = (),
    *,
    env: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> None:
    """Destroy every resource in the root's state; the caller has already confirmed it."""
    args = [
        "destroy",
        "-auto-approve",
        "-input=false",
        "-no-color",
        LOCK_TIMEOUT,
        f"-var-file={var_file}",
    ]
    for name, value in extra_vars:
        args += ["-var", f"{name}={value}"]
    _run(runner, "destroy", args, root_dir, env, secrets)


def terraform_outputs(
    runner: TerraformRunner,
    root_dir: Path,
    *,
    env: dict[str, str] | None = None,
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    result = _run(runner, "output", ["output", "-json"], root_dir, env, secrets)
    try:
        found = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        found = None
    if not isinstance(found, dict):
        raise Failure(ExitCode.TOOL_FAILED, f"terraform output in {root_dir.name} printed no JSON")
    return found


@dataclass(frozen=True)
class TerraformRoot:
    """One root module `deploy` plans and applies: its directory, state key and inputs.

    `var_file` is relative to `root_dir`. `env` holds provider settings the root needs beyond
    the job's cloud login, and `secrets` the values among them read from the secrets store,
    which a failure's error output masks; both are kept out of `repr` so no log line prints
    them.
    """

    name: str
    root_dir: Path
    key: str
    var_file: str
    extra_vars: tuple[tuple[str, str], ...] = ()
    env: dict[str, str] | None = field(default=None, compare=False, repr=False)
    secrets: tuple[str, ...] = field(default=(), compare=False, repr=False)


@dataclass(frozen=True)
class PlannedRoot:
    root: TerraformRoot
    plan_file: Path
    text: str


def plan_root(
    runner: TerraformRunner,
    root: TerraformRoot,
    *,
    backend: Mapping[str, str],
    out_dir: Path,
    refresh: bool = True,
) -> PlannedRoot:
    """Init `root` against its `backend` and plan it into `out_dir/<name>.tfplan`."""
    init_backend(runner, root.root_dir, backend, env=root.env, secrets=root.secrets)
    plan_file = out_dir / f"{root.name}.tfplan"
    text = terraform_plan(
        runner,
        root.root_dir,
        root.var_file,
        plan_file,
        root.extra_vars,
        env=root.env,
        secrets=root.secrets,
        refresh=refresh,
    )
    return PlannedRoot(root, plan_file, text)


def apply_root(runner: TerraformRunner, planned: PlannedRoot) -> None:
    root = planned.root
    terraform_apply(runner, root.root_dir, planned.plan_file, env=root.env, secrets=root.secrets)
