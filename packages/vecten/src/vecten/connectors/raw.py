"""`raw schema`: the tables and columns a sync landed, for the model skill."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from vecten.connectors.names import check_connector_name
from vecten.connectors.run import local_runtime, make_destination, warehouse_client
from vecten_platform.sync import RuntimeContext, set_runtime
from vecten_platform.sync.runner import dataset_name

COLUMNS_SQL = (
    "select table_name, column_name, data_type from information_schema.columns "
    "where lower(table_schema) = %s order by table_name, ordinal_position"
)


def schema_pipeline_name(name: str) -> str:
    # Object pipelines are `<connector>__<object>`; this prefix keeps an object named
    # `schema` from sharing this pipeline's state.
    return f"vecten_schema__{name}"


@contextmanager
def sql_client_for(ctx: RuntimeContext, name: str):
    # After vecten_platform.sync, which switches dlt telemetry off before dlt first loads.
    import dlt

    pipeline = dlt.pipeline(
        schema_pipeline_name(name),
        destination=make_destination(ctx),
        dataset_name=dataset_name(name),
        pipelines_dir=str(ctx.dlt_dir),
    )
    with warehouse_client(pipeline) as client:
        yield client


def raw_schema(project_root: Path, name: str) -> dict:
    check_connector_name(name)
    ctx = local_runtime(project_root)
    set_runtime(ctx)
    try:
        with sql_client_for(ctx, name) as client:
            rows = client.execute_sql(COLUMNS_SQL, dataset_name(name)) or []
    finally:
        set_runtime(None)
    tables: dict[str, list[dict]] = {}
    for table, column, data_type in rows:
        if str(table).lower().startswith("_dlt"):
            continue
        tables.setdefault(str(table).lower(), []).append(
            {"name": str(column).lower(), "type": str(data_type)}
        )
    return {
        "source": name,
        "schema": dataset_name(name),
        "tables": [{"name": t, "columns": cols} for t, cols in tables.items()],
    }
