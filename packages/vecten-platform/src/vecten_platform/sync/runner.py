"""One dlt pipeline per object: an object's checkpoint and failure are its own."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import dlt

from vecten_platform.sync.recording import record_to
from vecten_shared.output import Failure


@dataclass(frozen=True)
class ObjectOutcome:
    object: str
    ok: bool
    rows: int
    error: str | None = None
    # The normalised top-level tables the load wrote to, which `rows` counts.
    tables: tuple[str, ...] = ()


def pipeline_name(connector: str, object_name: str) -> str:
    return f"{connector}__{object_name}"


def dataset_name(connector: str) -> str:
    return f"raw_{connector}"


def ensure_datasets(connector: str, destination, dlt_dir: Path) -> list[str]:
    """Create the connector's raw schema and dlt's staging schema beside it, when missing.

    Each object's pipeline creates a missing schema itself, and objects load in parallel, so
    the first sync of a connector would race on creating them; running this once before the
    objects start leaves them nothing to create.
    """
    pipeline = dlt.pipeline(
        f"{connector}__datasets",
        destination=destination,
        dataset_name=dataset_name(connector),
        pipelines_dir=str(dlt_dir),
    )
    with pipeline.sql_client() as client:
        for staging in (False, True):
            name = client.fully_qualified_dataset_name(staging=staging)
            client.execute_sql(f"CREATE SCHEMA IF NOT EXISTS {name}")
        return [client.dataset_name, client.staging_dataset_name]


def list_objects(source) -> list[str]:
    return list(source.resources.keys())


def top_level_counts(row_counts: dict[str, int]) -> dict[str, int]:
    """Rows per data table an object's items landed in, without dlt's own or child tables."""
    return {
        table: n
        for table, n in row_counts.items()
        if not table.startswith("_dlt") and "__" not in table
    }


def _landed(pipeline) -> dict[str, int]:
    info = pipeline.last_trace.last_normalize_info if pipeline.last_trace else None
    return top_level_counts(info.row_counts if info else {})


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception and what it was raised from, outermost first, honouring `from None`."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return chain


def _describe(exc: BaseException) -> str:
    """One line naming the innermost exception, beneath dlt's pipeline-step wrappers."""
    inner = _chain(exc)[-1]
    return " ".join(f"{type(inner).__name__}: {inner}".split())


def run_object(
    connector: str,
    make_source: Callable[[], object],
    object_name: str,
    destination,
    dlt_dir: Path,
    record_dir: Path | None = None,
) -> ObjectOutcome:
    pipeline = dlt.pipeline(
        pipeline_name(connector, object_name),
        destination=destination,
        dataset_name=dataset_name(connector),
        pipelines_dir=str(dlt_dir),
    )
    recording = None
    try:
        source = make_source()
        if object_name not in source.resources:
            known = ", ".join(source.resources.keys())
            return ObjectOutcome(object_name, False, 0, f"unknown object; the source has: {known}")
        selected = source.with_resources(object_name)
        if record_dir is not None:
            recording = record_to(record_dir / f"{object_name}.jsonl")
            selected.resources[object_name].add_map(recording)
            # A fixture taken behind an incremental cursor would hold only the new rows, so a
            # recording truncates the object's tables, resets its state and reloads everything.
            pipeline.run(selected, refresh="drop_data")
        else:
            pipeline.run(selected)
    except Exception as exc:
        if recording is not None:
            recording.discard()
        # dlt wraps whatever a resource body raises in PipelineStepFailed, Ctrl-C included;
        # an interrupt must stop the run, not count as one object's failure.
        interrupt = next((e for e in _chain(exc) if not isinstance(e, Exception)), None)
        if interrupt is not None:
            raise interrupt from None
        # A missing secret or provider problem is a precondition, not a source failure, even
        # when a resource body raised it and dlt wrapped it.
        failure = next((e for e in _chain(exc) if isinstance(e, Failure)), None)
        if failure is not None:
            raise failure from None
        return ObjectOutcome(object_name, False, 0, _describe(exc))
    except BaseException:
        if recording is not None:
            recording.discard()
        raise
    if recording is not None:
        recording.commit()
    landed = _landed(pipeline)
    return ObjectOutcome(object_name, True, sum(landed.values()), tables=tuple(sorted(landed)))


def run_objects(
    connector: str,
    make_source: Callable[[], object],
    objects: list[str],
    destination,
    dlt_dir: Path,
    record_dir: Path | None = None,
) -> list[ObjectOutcome]:
    return [
        run_object(connector, make_source, name, destination, dlt_dir, record_dir)
        for name in objects
    ]
