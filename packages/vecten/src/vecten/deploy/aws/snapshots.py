"""The final RDS snapshots `destroy` takes before Terraform deletes the instances."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from botocore.exceptions import WaiterError

from vecten.deploy.aws.clients import aws_errors, aws_failure, aws_reason, error_code
from vecten.deploy.names import rds_identifier, snapshot_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs

_INSTANCE_GONE = "DBInstanceNotFound"
# A large instance's first snapshot copies every block; two hours bounds the wait.
WAIT_DELAY = 30
WAIT_ATTEMPTS = 240


def final_snapshots(
    rds: Any,
    project: str,
    env: str,
    outputs: EnvironmentOutputs,
    now: datetime,
    *,
    on_created: Callable[[str], None] | None = None,
    absent: list[str] | None = None,
) -> list[str]:
    """Snapshot every instance in the outputs' `postgres` map and wait until each is
    available; returns the snapshot identifiers.

    Only the instances the outputs name are snapshotted: the module creates an instance per
    key there, and predicting keys from tfvars would miss or invent one. An instance that no
    longer exists holds no data to keep, so its identifier is appended to `absent` instead.
    """
    taken: list[str] = []
    for key in sorted(outputs.postgres):
        instance = rds_identifier(project, env, key)
        snapshot = snapshot_name(project, env, key, now)
        try:
            rds.create_db_snapshot(DBSnapshotIdentifier=snapshot, DBInstanceIdentifier=instance)
        except aws_errors() as exc:
            if error_code(exc) == _INSTANCE_GONE:
                if absent is not None:
                    absent.append(instance)
                continue
            raise aws_failure(f"RDS CreateDBSnapshot of {instance}", exc) from None
        taken.append(snapshot)
        if on_created is not None:
            on_created(snapshot)
    waiter = rds.get_waiter("db_snapshot_available")
    for snapshot in taken:
        try:
            waiter.wait(
                DBSnapshotIdentifier=snapshot,
                WaiterConfig={"Delay": WAIT_DELAY, "MaxAttempts": WAIT_ATTEMPTS},
            )
        except (WaiterError, *aws_errors()) as exc:
            # Nothing has been deleted yet; the snapshot may still finish on its own.
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"final snapshot {snapshot} did not become available ({aws_reason(exc)}); "
                "nothing was destroyed",
            ) from None
    return taken
