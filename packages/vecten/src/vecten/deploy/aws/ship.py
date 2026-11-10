"""Shipping a bundle to one AWS host: S3 upload, SSM Run Command, health poll, rollback.

The host's instance role reads `bundles/<host>/*` from the environment's bucket; SSM runs the
download, the extraction and the agent as root. Nothing here passes a secret: the commands
name the bundle and the release directory only, and the agent reads secrets on the VM.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vecten.deploy.aws.clients import aws_errors as _aws_errors
from vecten.deploy.aws.clients import aws_failure as _aws_failure
from vecten.deploy.aws.clients import aws_reason
from vecten.deploy.aws.clients import error_code as _error_code
from vecten.deploy.shipping import (
    AGENT_LOG_PREFIX,
    FETCH_TIMEOUT,
    HEALTH,
    POLL_INTERVAL,
    Health,
    airflow_healthy,
    answers_200,
    bundle_key,
    component_url,
    container_log_message,
    fetch_response,
    host_for,
    log_excerpt,
    poll,
    ship_order,
)
from vecten_shared.output import ExitCode, Failure

__all__ = [
    "FETCH_TIMEOUT",
    "HEALTH",
    "POLL_INTERVAL",
    "Health",
    "airflow_healthy",
    "answers_200",
    "bundle_key",
    "component_url",
    "fetch_response",
    "host_for",
    "poll",
    "ship_host",
    "ship_order",
]

if TYPE_CHECKING:
    from vecten.deploy.aws import AwsProvider

RELEASES = "/opt/vecten/releases"
INCOMING = "/opt/vecten/incoming"
SSM_DOCUMENT = "AWS-RunShellScript"
SSM_TIMEOUT_SECONDS = 1800
SSM_POLL_SECONDS = 5
# SendCommand's timeout bounds delivery and executionTimeout bounds the run, so a command can
# stay non-terminal for both together before SSM ends it.
SSM_MAX_POLLS = 2 * SSM_TIMEOUT_SECONDS // SSM_POLL_SECONDS + 12
SSM_TERMINAL = frozenset({"Success", "Cancelled", "TimedOut", "Failed"})
# A host Terraform just created answers InvalidInstanceId until its SSM agent registers.
UNREGISTERED_WAIT_SECONDS = 600
UNREGISTERED_RETRY_SECONDS = 15
# User data installs Docker and the plugins the agent relies on; the agent must not race it.
# cloud-init exits 1 on an error and 2 when done with recoverable warnings; a host without it,
# or with it disabled, has no user data to wait for.
WAIT_FOR_USER_DATA = (
    "if command -v cloud-init >/dev/null 2>&1; then "
    "cloud-init status --wait >/dev/null 2>&1 && cloud_init=0 || cloud_init=$?; "
    'if [ "$cloud_init" -eq 1 ]; then '
    "echo 'vecten ship: cloud-init reported an error in user data; "
    "see /var/log/cloud-init-output.log on the host' >&2; exit 1; fi; fi"
)
FIRST_SHIP_WINDOW = 600
SHIP_WINDOW = 300
# SSM output is untrusted: only a line the agent or the ship script printed is reported, and
# the agent's lines name a failed step without quoting any value.
REPORTED_PREFIXES = ("vecten agent ", "vecten ship: ")
REPORTED_LIMIT = 300
# The clause the agent appends to its line once it has put the previous release back.
AGENT_ROLLED_BACK = "; rolled back to "
REDACTED = "***"

sleep = time.sleep
_fetched: set[str] = set()


def remember_secret(value: str) -> None:
    """Record a value read in this process so no reported line can carry it."""
    if value:
        _fetched.add(value)


def release_dir(commit: str) -> str:
    return f"{RELEASES}/{commit}"


def _reported_failure(step: str) -> str:
    return f"{{ echo {shlex.quote(f'vecten ship: {step} failed')} >&2; exit 1; }}"


def ship_commands(bucket: str, region: str, host: str, commit: str) -> list[str]:
    release = shlex.quote(release_dir(commit))
    archive = shlex.quote(f"{INCOMING}/{commit}.tar.gz")
    source = shlex.quote(f"s3://{bucket}/{bundle_key(host, commit)}")
    return [
        WAIT_FOR_USER_DATA,
        "set -eu",
        f"mkdir -p {INCOMING} {release}",
        f"aws s3 cp {source} {archive} --region {shlex.quote(region)} --only-show-errors "
        f"|| {_reported_failure('download')}",
        f"tar -xzf {archive} -C {release} --no-same-owner || {_reported_failure('extract')}",
        f"rm -f {archive}",
        f"python3 {release}/.vecten/ship/apply.py ship --release {release}",
    ]


def rollback_commands(commit: str) -> list[str]:
    # The agent of the release that failed its poll points `current` back at `previous`.
    release = shlex.quote(release_dir(commit))
    return ["set -eu", f"python3 {release}/.vecten/ship/apply.py rollback"]


def agent_line(stderr: str, known: Iterable[str] = ()) -> str | None:
    """The last line the agent or the ship script printed, redacted and cut to 300 characters."""
    values = sorted((v for v in known if v), key=len, reverse=True)
    for raw in reversed(stderr.splitlines()):
        line = raw.strip()
        if line.startswith(REPORTED_PREFIXES):
            for value in values:
                line = line.replace(value, REDACTED)
            return line[:REPORTED_LIMIT]
    return None


def agent_log(stderr: str, known: Iterable[str] = ()) -> list[str]:
    """The failing containers' log lines the agent printed, redacted."""
    lines = stderr.splitlines()
    tagged = [x[len(AGENT_LOG_PREFIX) :] for x in lines if x.startswith(AGENT_LOG_PREFIX)]
    return log_excerpt(tagged, known)


@dataclass(frozen=True)
class Invocation:
    status: str
    stderr: str


def _send(ssm: Any, instance_id: str, commands: list[str], comment: str) -> dict:
    """SendCommand, retried while a fresh instance's SSM agent has not registered yet.

    A failure carries the exception type alone in `details["reason"]`.
    """
    errors = _aws_errors()
    tries = UNREGISTERED_WAIT_SECONDS // UNREGISTERED_RETRY_SECONDS + 1
    for attempt in range(1, tries + 1):
        try:
            return ssm.send_command(
                InstanceIds=[instance_id],
                DocumentName=SSM_DOCUMENT,
                TimeoutSeconds=SSM_TIMEOUT_SECONDS,
                Parameters={"commands": commands, "executionTimeout": [str(SSM_TIMEOUT_SECONDS)]},
                Comment=comment[:100],
            )
        except errors as exc:
            reason = aws_reason(exc)
            if _error_code(exc) != "InvalidInstanceId":
                raise Failure(
                    ExitCode.TOOL_FAILED,
                    f"SSM SendCommand to {instance_id} failed: {reason}",
                    details={"reason": reason},
                ) from None
            if attempt == tries:
                raise Failure(
                    ExitCode.TOOL_FAILED,
                    f"instance {instance_id} did not register with SSM within "
                    f"{UNREGISTERED_WAIT_SECONDS} s",
                    details={"reason": reason},
                ) from None
            sleep(UNREGISTERED_RETRY_SECONDS)
    raise Failure(ExitCode.INTERNAL, "SendCommand retry loop ended without an answer")


def run_on_host(ssm: Any, instance_id: str, commands: list[str], comment: str) -> Invocation:
    """Send `commands` through SSM Run Command and wait until the invocation is terminal."""
    errors = _aws_errors()
    response = _send(ssm, instance_id, commands, comment)
    command_id = response["Command"]["CommandId"]
    for _ in range(SSM_MAX_POLLS):
        try:
            found = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except errors as exc:
            # The invocation is registered a moment after SendCommand returns.
            if _error_code(exc) != "InvocationDoesNotExist":
                raise _aws_failure(f"SSM GetCommandInvocation on {instance_id}", exc) from None
            sleep(SSM_POLL_SECONDS)
            continue
        status = found.get("Status", "")
        if status in SSM_TERMINAL:
            return Invocation(status=status, stderr=found.get("StandardErrorContent") or "")
        sleep(SSM_POLL_SECONDS)
    raise Failure(
        ExitCode.TOOL_FAILED,
        f"SSM command {command_id} on {instance_id} did not finish within "
        f"{SSM_MAX_POLLS * SSM_POLL_SECONDS} s",
    )


def first_ship(s3: Any, bucket: str, host: str, commit: str) -> bool:
    """True when the bucket holds no bundle of this host from another commit."""
    try:
        listing = s3.list_objects_v2(Bucket=bucket, Prefix=f"bundles/{host}/", MaxKeys=2)
    except _aws_errors() as exc:
        raise _aws_failure("S3 ListObjectsV2", exc) from None
    own = bundle_key(host, commit)
    return not any(entry["Key"] != own for entry in listing.get("Contents", []))


def upload(s3: Any, bucket: str, host: str, commit: str, bundle: Path) -> str:
    key = bundle_key(host, commit)
    try:
        with bundle.open("rb") as handle:
            s3.put_object(Bucket=bucket, Key=key, Body=handle)
    except _aws_errors() as exc:
        raise _aws_failure("S3 PutObject", exc) from None
    return key


def _invocation_failure(host: str, invocation: Invocation) -> Failure:
    line = agent_line(invocation.stderr, _fetched)
    log = agent_log(invocation.stderr, _fetched)
    message = f"shipping to host {host} failed ({invocation.status})"
    message = f"{message}: {line}" if line else message
    return Failure(
        ExitCode.TOOL_FAILED,
        message + container_log_message(log),
        details={
            "host": host,
            "status": invocation.status,
            "rolled_back": bool(line) and AGENT_ROLLED_BACK in line,
            "container_log": log,
        },
    )


def ship_host(provider: AwsProvider, host: str, bundle: Path, commit: str) -> None:
    """Upload `bundle`, run the agent on `host`, poll its components, roll back a failed poll.

    A failed agent run is not rolled back from here: the agent restores the previous release
    itself when it fails after its swap, and leaves `current` alone before it; its line says
    which happened.
    """
    outputs = provider.outputs()
    target = outputs.hosts[host]
    s3 = provider.client("s3")
    first = first_ship(s3, outputs.bucket, host, commit)
    upload(s3, outputs.bucket, host, commit, bundle)
    ssm = provider.client("ssm")
    commands = ship_commands(outputs.bucket, outputs.region, host, commit)
    invocation = run_on_host(ssm, target.instance_id, commands, f"vecten ship {host} {commit}")
    if invocation.status != "Success":
        raise _invocation_failure(host, invocation)
    window = FIRST_SHIP_WINDOW if first else SHIP_WINDOW
    for component in target.components:
        url = component_url(outputs, component)
        if poll(url, window, healthy=HEALTH[component].healthy):
            continue
        message = f"{component} did not answer healthy at {url} within {window} s; "
        log: list[str] = []
        try:
            undo = run_on_host(
                ssm, target.instance_id, rollback_commands(commit), f"vecten rollback {host}"
            )
        except Failure as exc:
            # The poll failure is what the operator acts on; the rollback's own failure is
            # reported beside it, by exception type when SSM refused the command.
            rolled_back = False
            reason = exc.details.get("reason") or exc.message
            message += f"rollback could not be sent: {reason}"
        else:
            rolled_back = undo.status == "Success"
            log = agent_log(undo.stderr, _fetched)
            if rolled_back:
                message += f"host {host} is back on its previous bundle"
            else:
                line = agent_line(undo.stderr, _fetched)
                message += f"the rollback on host {host} failed too ({undo.status})"
                message += f": {line}" if line else ""
        raise Failure(
            ExitCode.TOOL_FAILED,
            message + container_log_message(log),
            details={
                "component": component,
                "host": host,
                "url": url,
                "rolled_back": rolled_back,
                "container_log": log,
            },
        )
