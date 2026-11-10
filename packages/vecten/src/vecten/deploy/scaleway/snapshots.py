"""The final RDB snapshots `destroy` takes before Terraform deletes the instances.

Destroy is safe only while Scaleway keeps an instance's snapshots after the instance is
deleted. The fakes here take that as given; the release procedure's Scaleway destroy and restore
checks it against a live account. Paths,
from the Scaleway SDK generated from the API definitions (`scaleway-sdk-python`,
`scaleway/rdb/v1/api.py`, `types.py`, `content.py`):

- `POST /rdb/v1/regions/{region}/instances/{instance_id}/snapshots` with `name` (and an
  optional `expires_at`);
- `GET /rdb/v1/regions/{region}/snapshots/{snapshot_id}`, whose `status` is `creating` until
  `ready`, or `error`, `locked`, `unknown`, and whose `expires_at` says when Scaleway deletes it.

Scaleway keeps a snapshot at most 365 days and refuses a later `expires_at`; one created
without it gets those 365 days, so destroy sends none and reports the date instead. Keeping
the data longer means restoring the snapshot and taking a new one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote

from vecten.deploy.names import rds_identifier, snapshot_name
from vecten.deploy.scaleway.clients import ApiError
from vecten.deploy.scaleway.rdb import find_instance
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs

# A large instance's first snapshot copies every block; two hours bounds the wait.
WAIT_DELAY = 30
WAIT_ATTEMPTS = 240
PENDING = frozenset({"creating"})
READY = "ready"
STATUSES = frozenset({"unknown", "creating", "ready", "restoring", "deleting", "error", "locked"})

sleep = time.sleep


def final_snapshots(
    rdb: Any,
    project: str,
    env: str,
    project_id: str,
    outputs: EnvironmentOutputs,
    now: datetime,
    *,
    on_created: Callable[[str], None],
    absent: list[str],
    expires: dict[str, str],
) -> list[str]:
    """Snapshot every instance in the outputs' `postgres` map and wait until each is ready;
    returns the snapshot names, and records in `expires` when Scaleway deletes each.

    An instance that no longer exists holds no data to keep, so its name is appended to
    `absent` instead.
    """
    taken: list[tuple[str, str]] = []
    for key in sorted(outputs.postgres):
        name = rds_identifier(project, env, key)
        snapshot = snapshot_name(project, env, key, now)
        instance = find_instance(rdb, project_id, name)
        if instance is None:
            absent.append(name)
            continue
        try:
            created = rdb.request(
                "POST",
                f"/instances/{quote(str(instance['id']), safe='')}/snapshots",
                json={"name": snapshot},
            )
        except ApiError as error:
            if error.status != 404:
                raise
            absent.append(name)
            continue
        taken.append((snapshot, str(created["id"])))
        on_created(snapshot)
    for snapshot, snapshot_id in taken:
        expires_at = _wait_ready(rdb, snapshot, snapshot_id).get("expires_at")
        if expires_at:
            expires[snapshot] = str(expires_at)
    return [snapshot for snapshot, _ in taken]


def _wait_ready(rdb: Any, snapshot: str, snapshot_id: str) -> dict[str, Any]:
    status = None
    for attempt in range(WAIT_ATTEMPTS):
        read = rdb.request("GET", f"/snapshots/{quote(snapshot_id, safe='')}")
        status = read.get("status")
        if status == READY:
            return read
        if status not in PENDING:
            break
        if attempt + 1 < WAIT_ATTEMPTS:
            sleep(WAIT_DELAY)
    # Nothing has been deleted yet; a pending snapshot may still finish on its own.
    if status in PENDING:
        reason = f"not within {WAIT_DELAY * WAIT_ATTEMPTS} s"
    else:
        reason = f"it is {status}" if status in STATUSES else "its status is unexpected"
    raise Failure(
        ExitCode.TOOL_FAILED,
        f"final snapshot {snapshot} did not become ready ({reason}); nothing was destroyed",
    )
