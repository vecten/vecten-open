"""The sync runtime shared by `vecten connector run` and the Airflow sync DAGs."""

from __future__ import annotations

import os

# dlt reads this before anything else runs; the product never phones home.
os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

# Vecten supplies dlt configuration and secrets without consulting Airflow Variables.
os.environ.setdefault("PROVIDERS__ENABLE_AIRFLOW_SECRETS", "false")

from vecten_platform.sync.context import RuntimeContext, runtime, secret, set_runtime  # noqa: E402


class TerminalConnectorError(Exception):
    """Raise during an object load when another attempt cannot succeed."""


def unit_cursor(default=None):
    """Read this resource's last committed cursor inside a dlt resource generator."""
    import dlt

    return dlt.current.resource_state().get("vecten_unit", {}).get("cursor", default)


def finish_unit(cursor: str | int | None, *, more: bool) -> None:
    """Commit the next cursor with this load and request another unit after it succeeds."""
    import dlt

    disposition = dlt.current.resource().write_disposition
    strategy = None
    if isinstance(disposition, dict):
        strategy = disposition.get("strategy")
        disposition = disposition.get("disposition")
    if disposition not in ("append", "merge") or strategy not in (
        None,
        "delete-insert",
        "upsert",
        "insert-only",
    ):
        raise ValueError("checkpointed units need an append or merge-by-key resource")
    if not isinstance(more, bool) or (cursor is not None and type(cursor) not in (int, str)):
        raise ValueError("finish_unit needs a string, integer, or null cursor and boolean more")
    previous = unit_cursor()
    if more and (cursor is None or cursor == previous):
        raise ValueError("another unit needs a new cursor")
    dlt.current.resource_state()["vecten_unit"] = {
        "cursor": cursor,
        "previous": previous,
        "more": more,
        "load_id": dlt.current.load_package_state()["load_id"],
    }


__all__ = [
    "finish_unit",
    "unit_cursor",
    "RuntimeContext",
    "TerminalConnectorError",
    "runtime",
    "secret",
    "set_runtime",
]
