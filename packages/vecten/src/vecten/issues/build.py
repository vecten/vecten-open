"""The two places `vecten issues` reads from: the laptop stack and a deployed environment.

On the laptop it reads the local Airflow API that `up` publishes on the loopback address, the
dbt results under `.vecten/artifacts` and the `.env` values. For an environment it reads the
deployed Airflow through its NGINX front door with the credentials in the secrets store, the
environment's bucket with the person's cloud login, and the MCP admin API with the
`vecten login` token.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from vecten.issues.airflow import AirflowClient, ApiLogs
from vecten.issues.context import (
    BucketLogs,
    DirectoryArtifacts,
    IssueContext,
    PrefixedReader,
    SourceUnavailable,
    TaskAttempt,
)
from vecten.stack.compose import LOCAL_ARTIFACTS, vm_hostnames
from vecten_shared.config import VectenConfig
from vecten_shared.envfile import read_env_values
from vecten_shared.issues import MIN_SECRET_LENGTH
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import read_record

ADMIN_USER = "admin"
# The VM's NGINX front door and Airflow's own login each have a password in the store.
AIRFLOW_SECRETS = ("AIRFLOW_ADMIN_PASSWORD",)
ARTIFACT_PREFIX = "platform/"
LOG_PREFIX = "platform/logs/"
WEBHOOK_SECRET = "ALERT_WEBHOOK_URL"


class UnreachableAirflow:
    """Stands in for Airflow when its credentials could not be read."""

    def __init__(self, reason: str):
        self.reason = reason

    def task_instances(self, since: datetime) -> list[TaskAttempt]:
        raise SourceUnavailable(self.reason)


class PlaneObjects:
    """The environment's bucket through the control plane, with failures as unreadable sources."""

    def __init__(self, plane: Any, bucket: str):
        self.plane, self.bucket = plane, bucket

    def read(self, key: str) -> bytes | None:
        try:
            return self.plane.read_object(key)
        except Failure as failure:
            raise SourceUnavailable(failure.message) from None

    def location(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"


def _admin(root: Path, env: str) -> Callable[[str, dict[str, str]], Any]:
    from vecten.mcp_client import AdminClient

    def read(operation: str, params: dict[str, str]) -> Any:
        try:
            return AdminClient(root, env).read(operation, params)
        except Failure as failure:
            raise SourceUnavailable(failure.message) from None

    return read


def _secret_values(values: list[str | None]) -> tuple[str, ...]:
    return tuple(sorted({v for v in values if v and len(v) >= MIN_SECRET_LENGTH}))


def laptop_context(root: Path, config: VectenConfig) -> IssueContext:
    env_file = read_env_values(root / ".env")
    airflow = logs = None
    if config.has("platform"):
        up = read_record(root, "up") or {}
        api = up.get("airflow_api")
        if not api:
            raise Failure(
                ExitCode.PREREQUISITE,
                "the local stack is not up; run `vecten up`, whose Airflow `vecten issues` reads",
            )
        airflow = AirflowClient(
            api,
            username=env_file.get("AIRFLOW_ADMIN_USER") or ADMIN_USER,
            password=env_file.get("AIRFLOW_ADMIN_PASSWORD") or "",
        )
        logs = ApiLogs(airflow)
    return IssueContext(
        root=root,
        config=config,
        env="local",
        airflow=airflow,
        artifacts=DirectoryArtifacts(root / LOCAL_ARTIFACTS),
        logs=logs,
        admin=_admin(root, "local") if config.has("mcp") else None,
        secrets=_secret_values(list(env_file.values())),
    )


def _read_secret(plane: Any, name: str) -> str | None:
    try:
        return plane.get_secret(name)
    except Failure:
        return None


def environment_context(root: Path, config: VectenConfig, env: str) -> IssueContext:
    from vecten.deploy.run import control_plane, current_outputs

    plane = control_plane(config, root, env)
    outputs = current_outputs(plane)
    if outputs is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"{env} has no outputs; `vecten deploy --env {env}` creates the environment first",
        )
    bucket = PlaneObjects(plane, outputs.bucket)
    airflow: Any = None
    passwords: list[str | None] = []
    if config.has("platform"):
        passwords = [_read_secret(plane, name) for name in AIRFLOW_SECRETS]
        (admin,) = passwords
        if admin is None:
            airflow = UnreachableAirflow(
                f"the Airflow credentials of {env} could not be read from the secrets store; "
                "check your cloud login"
            )
        else:
            host = vm_hostnames(outputs)["airflow"]
            airflow = AirflowClient(
                f"https://{host}",
                username=ADMIN_USER,
                password=admin,
            )
    names = sorted({s for c in config.connectors for s in c.secrets} | {WEBHOOK_SECRET})
    return IssueContext(
        root=root,
        config=config,
        env=env,
        airflow=airflow,
        artifacts=PrefixedReader(bucket, ARTIFACT_PREFIX),
        logs=BucketLogs(bucket, LOG_PREFIX),
        admin=_admin(root, env) if config.has("mcp") else None,
        secrets=_secret_values([*passwords, *(_read_secret(plane, n) for n in names)]),
    )
