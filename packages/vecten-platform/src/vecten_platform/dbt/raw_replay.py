"""Raw data for a local build: every connector's recorded fixtures, loaded into `raw_<name>`."""

from __future__ import annotations

from pathlib import Path

from vecten_platform.dbt.local_warehouse import LocalTarget

# Importing the sync package turns dlt telemetry off, so it precedes every dlt import here.
from vecten_platform.sync.fixtures import fixture_files
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure


def _fixtures_dir(connectors_dir: Path, connector: str) -> Path:
    return connectors_dir / connector / "fixtures"


def missing_fixtures(connectors_dir: Path, config: VectenConfig) -> list[str]:
    return [
        c.name
        for c in config.connectors
        if not fixture_files(_fixtures_dir(connectors_dir, c.name))
    ]


def _destination(target: LocalTarget):
    import dlt

    from vecten_platform.sync.destination import build_destination

    if target.kind == "snowflake":
        # The emulation keeps the target's database in this file, and DuckDB resolves identifiers
        # case-insensitively, so `raw_demo.company` answers the emulation's RAW_DEMO.COMPANY.
        return dlt.destinations.duckdb(
            str(target.workdir / "fakesnow" / f"{target.spec.database}.db")
        )
    return build_destination(target.spec, target.password)


def _top_level_tables(pipeline) -> list[str]:
    schema = pipeline.default_schema
    return sorted(
        name for name in schema.data_table_names() if schema.tables[name].get("parent") is None
    )


def replay_fixtures(
    target: LocalTarget, connectors_dir: Path, config: VectenConfig
) -> dict[str, list[str]]:
    import dlt
    from dlt.pipeline.exceptions import PipelineStepFailed

    from vecten_platform.sync.recording import replay_source
    from vecten_platform.sync.runner import dataset_name

    loaded: dict[str, list[str]] = {}
    for connector in config.connectors:
        name = connector.name
        fixtures_dir = _fixtures_dir(connectors_dir, name)
        if not fixture_files(fixtures_dir):
            loaded[name] = []
            continue
        pipeline = dlt.pipeline(
            pipeline_name=f"replay_{name}",
            destination=_destination(target),
            dataset_name=dataset_name(name),
            pipelines_dir=str(target.workdir / "dlt"),
        )
        # Fixture resources append, so the raw tables are dropped first to hold the fixtures only.
        try:
            pipeline.run(replay_source(fixtures_dir, name), refresh="drop_sources")
        except PipelineStepFailed as exc:
            raise Failure(
                ExitCode.TOOL_FAILED, f"replaying connector {name} failed: {exc}"
            ) from exc
        loaded[name] = _top_level_tables(pipeline)
    return loaded
