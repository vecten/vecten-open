"""Scaleway Managed Databases as the control plane reaches them through the RDB API: finding
an instance by the name the module gives it, and its public (load-balancer) endpoint.

Paths, from the Scaleway SDK generated from the API definitions
(`scaleway-sdk-python`, `scaleway/rdb/v1/api.py`):

- `GET /rdb/v1/regions/{region}/instances?project_id=&name=` lists instances;
- `GET /rdb/v1/regions/{region}/instances/{instance_id}` reads one, whose `status` is `ready`
  once no change is under way (`InstanceStatus`);
- `DELETE /rdb/v1/regions/{region}/endpoints/{endpoint_id}` deletes one endpoint;
- `PATCH /rdb/v1/regions/{region}/instances/{instance_id}/users/{name}` with `{"password"}`
  sets a user's password (`update_user`).

An endpoint whose `load_balancer` is set is the public one; a private endpoint carries
`private_network` instead.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from vecten.deploy.scaleway.clients import ApiError
from vecten_shared.output import ExitCode, Failure

PAGE_SIZE = 100
READY = "ready"
# Statuses an instance does not leave without someone's help, so waiting on them is pointless.
STUCK = frozenset({"error", "locked", "disk_full", "deleting"})
READY_WINDOW = 900
READY_POLL_SECONDS = 10

sleep = time.sleep
clock = time.monotonic


def find_instance(rdb: Any, project_id: str, name: str) -> dict[str, Any] | None:
    """The instance named exactly `name` in `project_id`; None when there is none.

    The `name` filter is not documented as exact, so the match is made here.
    """
    page = 1
    seen = 0
    while True:
        doc = rdb.request(
            "GET",
            "/instances",
            params={"project_id": project_id, "name": name, "page": page, "page_size": PAGE_SIZE},
        )
        batch = [i for i in doc.get("instances") or [] if isinstance(i, dict)]
        for instance in batch:
            if instance.get("name") == name and instance.get("id"):
                return instance
        seen += len(batch)
        total = doc.get("total_count")
        if not batch or not isinstance(total, int) or seen >= total:
            return None
        page += 1


def public_endpoints(instance: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        e
        for e in instance.get("endpoints") or []
        if isinstance(e, dict) and e.get("load_balancer") is not None
    ]


def close_public_endpoints(rdb: Any, project_id: str, names: list[str]) -> list[str]:
    """Delete every public endpoint of the instances named `names`; returns the names of the
    instances that had one. An instance or endpoint already gone is skipped."""
    closed: list[str] = []
    for name in names:
        instance = find_instance(rdb, project_id, name)
        if instance is None:
            continue
        endpoints = public_endpoints(instance)
        for endpoint in endpoints:
            try:
                rdb.request("DELETE", f"/endpoints/{quote(str(endpoint['id']), safe='')}")
            except ApiError as error:
                if error.status != 404:
                    raise
        if endpoints:
            closed.append(name)
    return closed


def wait_ready(rdb: Any, instance_id: str, name: str, *, timeout: int = READY_WINDOW) -> None:
    """Return once the instance named `name` reports `ready`; refuses, naming the instance and
    its last status, when it does not within `timeout` s or reaches a status it stays in.

    RDB refuses a user change while the instance is `configuring`, which deleting an endpoint
    leaves it in, and the setup agent cannot connect mid-configure either.
    """
    deadline = clock() + timeout
    while True:
        try:
            doc = rdb.request("GET", f"/instances/{quote(instance_id, safe='')}")
        except ApiError as error:
            if error.status != 404:
                raise
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"database instance {name} no longer exists",
                details={"instance": name},
            ) from None
        status = doc.get("status")
        if status == READY:
            return
        if status in STUCK or clock() >= deadline:
            reason = f"it is {status}" if isinstance(status, str) else "it reports no status"
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"database instance {name} did not become ready within {timeout} s ({reason})",
                details={"instance": name, "status": status},
            )
        sleep(READY_POLL_SECONDS)


def set_user_password(rdb: Any, instance_id: str, user: str, password: str) -> None:
    rdb.request(
        "PATCH",
        f"/instances/{quote(instance_id, safe='')}/users/{quote(user, safe='')}",
        json={"password": password},
    )
