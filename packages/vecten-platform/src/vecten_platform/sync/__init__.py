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

    resource = dlt.current.resource()
    tables = [
        t for t in dlt.current.source_schema().tables.values() if t.get("resource") == resource.name
    ]
    if not resource.has_dynamic_table_name:
        tables.append(resource.compute_table_schema())
    for table in tables:
        disposition = table.get("write_disposition", "append")
        columns = table["columns"].values()
        if (
            disposition not in ("append", "merge")
            or table.get("x-merge-strategy") not in (None, "delete-insert", "upsert", "insert-only")
            or table.get("x-merge-destination-scope") is not None
            or any(col.get("merge_key") for col in columns)
            or (disposition == "merge" and not any(col.get("primary_key") for col in columns))
        ):
            raise ValueError(
                "checkpointed units need an append or merge by primary key "
                "without a merge key or destination scope"
            )
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
