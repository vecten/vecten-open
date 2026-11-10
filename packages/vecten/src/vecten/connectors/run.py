"""`connector run`: the sync from the shell, with the same runtime the DAGs use."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vecten.connectors.names import check_connector_name, run_record_name
from vecten.connectors.render import scaffold_hash
from vecten.connectors.sources_yml import add_tables
from vecten_platform.sync import RuntimeContext, set_runtime
from vecten_platform.sync.destination import warehouse_destination
from vecten_platform.sync.loader import load_connector
from vecten_platform.sync.runner import ObjectOutcome, list_objects, run_objects
from vecten_shared.config import GENERIC_KINDS, load_config, save_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.local import LocalProvider
from vecten_shared.records import read_record, write_record


@dataclass
class RunResult:
    source: str
    objects: list[ObjectOutcome]
    recorded: bool
    tables: list[str]

    @property
    def ok(self) -> bool:
        return all(o.ok for o in self.objects)

    def as_data(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "recorded": self.recorded,
            "objects": {
                o.object: {"ok": o.ok, "rows": o.rows, "error": o.error} for o in self.objects
            },
            "tables": self.tables,
        }


def local_runtime(project_root: Path) -> RuntimeContext:
    return RuntimeContext(
        project_root=project_root,
        connectors_dir=project_root / "connectors",
        env="local",
        provider=LocalProvider(project_root, inside_compose=False),
        dlt_dir=project_root / ".vecten" / "dlt",
    )


def make_destination(ctx: RuntimeContext):
    return warehouse_destination(ctx)


UNREACHABLE = "the warehouse is not reachable; run `vecten up`"


@contextmanager
def warehouse_client(pipeline) -> Iterator:
    """The pipeline's SQL client, connected; a refused connection means the stack is down."""
    client = pipeline.sql_client()
    try:
        client.open_connection()
        client.execute_sql("select 1")
    except Exception as exc:
        raise Failure(ExitCode.PREREQUISITE, f"{UNREACHABLE} ({type(exc).__name__})") from None
    try:
        yield client
    finally:
        client.close_connection()


def probe_warehouse(destination, dlt_dir: Path) -> None:
    # After vecten_platform.sync, which switches dlt telemetry off before dlt first loads.
    import dlt

    pipeline = dlt.pipeline(
        "vecten_probe",
        destination=destination,
        dataset_name="vecten_probe",
        pipelines_dir=str(dlt_dir),
    )
    with warehouse_client(pipeline):
        pass


def _flip_edited_scaffold(project_root: Path, name: str) -> None:
    """A generic scaffold whose body changed is the user's code now, so it becomes custom."""
    config = load_config(project_root)
    decl = config.connector(name)
    if decl is None or decl.source not in GENERIC_KINDS or not decl.scaffold_hash:
        return
    connector_py = project_root / "connectors" / name / "connector.py"
    if scaffold_hash(connector_py.read_text(encoding="utf-8")) != decl.scaffold_hash:
        decl.source = "custom"
        decl.scaffold_hash = None
        save_config(project_root, config)


def run_connector(
    project_root: Path, name: str, object_name: str | None, record: bool
) -> RunResult:
    check_connector_name(name)
    ctx = local_runtime(project_root)
    set_runtime(ctx)
    try:
        module = load_connector(ctx.connectors_dir, name)
        destination = make_destination(ctx)
        probe_warehouse(destination, ctx.dlt_dir)
        _flip_edited_scaffold(project_root, name)
        try:
            available = list_objects(module.source())
        except Failure:
            raise
        except Exception as exc:
            write_record(
                project_root,
                run_record_name(name),
                {"source": name, "ok": False, "recorded": False, "objects": {}},
            )
            raise Failure(
                ExitCode.TOOL_FAILED, f"{name}: source() failed: {type(exc).__name__}: {exc}"
            ) from None
        if object_name is not None and object_name not in available:
            raise Failure(
                ExitCode.USAGE,
                f"{name} has no object {object_name!r}; objects: {', '.join(available)}",
            )
        objects = [object_name] if object_name else available
        record_dir = project_root / "connectors" / name / "fixtures" if record else None
        outcomes = run_objects(name, module.source, objects, destination, ctx.dlt_dir, record_dir)
    finally:
        set_runtime(None)
    landed = sorted({table for o in outcomes if o.ok for table in o.tables})
    tables = add_tables(project_root / "dbt", name, landed) if landed else []
    result = RunResult(name, outcomes, record, tables)
    write_record(
        project_root, run_record_name(name), _merged_record(project_root, result, available)
    )
    return result


def _merged_record(project_root: Path, result: RunResult, available: list[str]) -> dict:
    """The run record across runs: an `--object` run updates its object and keeps the others."""
    data = result.as_data()
    previous = (read_record(project_root, run_record_name(result.source)) or {}).get("objects")
    earlier = previous if isinstance(previous, dict) else {}
    merged = {**earlier, **data["objects"]}
    data["objects"] = {o: merged[o] for o in available if o in merged}
    data["ok"] = all(entry.get("ok") for entry in data["objects"].values())
    return data
