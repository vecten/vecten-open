"""The sizing table: per cloud and tier, the VM and warehouse classes and the connection budget.

`sizing.json` is the one table. `terraform/aws/sizing.json` and `terraform/scaleway/sizing.json`
are its cloud slices, which the cloud modules decode, and `terraform/snowflake/sizing.json` its
Snowflake warehouse sizes, all held byte-identical by a test. `max_connections` sits below the
managed Postgres default for the class (on RDS `DBInstanceClassMemory / 9531392`), so the budget
holds without a parameter group override; `state_max_connections` is the same for the state
class. Scaleway publishes no per-node default, so its figures are conservative guesses until read
from a live instance. `minimal_class` is the class of the minimal profile's single instance: the
warehouse class on AWS, and on Scaleway small a larger class than the warehouse's, because that
instance also carries the state databases' connections. `users_max` of `small` and `medium` are
the intake's thresholds. Scaleway volumes are fixed in size, so its storage ceiling equals the
volume.
"""

from __future__ import annotations

import copy
import json
from functools import cache
from importlib import resources
from typing import Any

from vecten_shared.output import ExitCode, Failure

TIERS: tuple[str, ...] = ("small", "medium", "large")


@cache
def _table() -> dict[str, Any]:
    text = resources.files("vecten.deploy").joinpath("sizing.json").read_text(encoding="utf-8")
    return json.loads(text)


def _clouds() -> list[str]:
    return [k for k in _table() if k != "snowflake_warehouse"]


def _cloud(cloud: str) -> dict[str, Any]:
    if cloud not in _clouds():
        raise Failure(
            ExitCode.CONFIG, f"cloud must be one of: {', '.join(_clouds())}; got {cloud!r}"
        )
    return _table()[cloud]


def _check_size(size: str) -> None:
    if size not in TIERS:
        raise Failure(ExitCode.CONFIG, f"size must be one of: {', '.join(TIERS)}; got {size!r}")


def tier(cloud: str, size: str) -> dict[str, Any]:
    entry = _cloud(cloud)
    _check_size(size)
    return dict(entry["tiers"][size])


def state_class(cloud: str) -> str:
    return _cloud(cloud)["state_class"]


def minimal_vm(cloud: str) -> str:
    return _cloud(cloud)["minimal_vm"]


def snowflake_warehouse_size(size: str) -> str:
    _check_size(size)
    return _table()["snowflake_warehouse"][size]


def aws_slice() -> dict[str, Any]:
    return copy.deepcopy(_cloud("aws"))


def scaleway_slice() -> dict[str, Any]:
    return copy.deepcopy(_cloud("scaleway"))


def snowflake_slice() -> dict[str, str]:
    return dict(_table()["snowflake_warehouse"])
