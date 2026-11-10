"""The sync DAG: discover objects at run time, one mapped task per object, one asset per group.

The loader re-imports `connectors.<name>` on every call, so a process must hold one
connector of a given name: one DAG file per dag-processor subprocess and one connector per
task process keep two projects' same-named connectors from resolving against each other.
"""

from __future__ import annotations

from datetime import timedelta

from vecten_platform.sync.alerts import alert_on_failure
from vecten_platform.sync.context import runtime
from vecten_platform.sync.destination import warehouse_destination
from vecten_platform.sync.loader import load_connector
from vecten_platform.sync.runner import ensure_datasets, list_objects, run_object
from vecten_shared.output import Failure

ASSET_SCHEME = "vecten"


def discover_objects(connector: str, objects: list[str]) -> list[str]:
    if objects:
        return list(objects)
    ctx = runtime()
    module = load_connector(ctx.connectors_dir, connector)
    return list_objects(module.source())


def ensure_schemas(connector: str) -> None:
    ctx = runtime()
    ensure_datasets(connector, warehouse_destination(ctx), ctx.dlt_dir)


class ObjectSyncError(RuntimeError):
    """One object's failed load. The message keeps the source's own exception class second,
    `<connector>.<object>: <Class>: <detail>`, which `vecten issues` and the alert group by."""

    def __init__(self, connector: str, object_name: str, error: str):
        super().__init__(f"{connector}.{object_name}: {error}")
        self.connector, self.object_name = connector, object_name


def _describe(exc: Exception) -> str:
    message = exc.message if isinstance(exc, Failure) else str(exc)
    return " ".join(f"{type(exc).__name__}: {message}".split())


def sync_one(connector: str, object_name: str) -> dict:
    try:
        ctx = runtime()
        module = load_connector(ctx.connectors_dir, connector)
        outcome = run_object(
            connector, module.source, object_name, warehouse_destination(ctx), ctx.dlt_dir
        )
    except Exception as exc:
        # A missing secret or a connector that does not import fails one object's task too,
        # and the alert and `vecten issues` key every such failure by its object.
        raise ObjectSyncError(connector, object_name, _describe(exc)) from exc
    if not outcome.ok:
        raise ObjectSyncError(connector, object_name, outcome.error or "failed")
    return {"object": object_name, "rows": outcome.rows}


def failed_objects(objects: list[str], states: dict[str, str | None]) -> list[str]:
    """`states` is keyed `sync_<map_index>`, and map index i is the i-th discovered object."""
    passed = {"success", "skipped"}
    return [name for i, name in enumerate(objects) if states.get(f"sync_{i}") not in passed]


def raise_for_failed(connector: str, objects: list[str], states: dict[str, str | None]) -> None:
    failed = failed_objects(objects, states)
    if failed:
        raise RuntimeError(
            f"{connector}: {len(failed)} of {len(objects)} objects failed: {', '.join(failed)}"
        )


def asset_for(connector: str, group: str):
    from airflow.sdk import Asset

    return Asset(name=f"raw_{connector}.{group}", uri=f"{ASSET_SCHEME}://raw_{connector}/{group}")


def build_sync_dag(
    connector: str,
    group: str,
    objects: list[str],
    schedule: object = "@daily",
    retry_delay: timedelta = timedelta(minutes=5),  # noqa: B008
):
    # Airflow lives only in the Airflow image; importing it here keeps `connector run` light.
    from airflow.sdk import dag as dag_decorator
    from airflow.sdk import get_current_context, task
    from airflow.sdk.definitions.timetables.interval import CronDataIntervalTimetable

    if isinstance(schedule, str):
        # A plain cron string first runs at the next boundary; without catchup, this one runs
        # the latest whole interval once the DAG is unpaused, so a fresh deploy syncs at once.
        schedule = CronDataIntervalTimetable(schedule, timezone="UTC")

    @dag_decorator(
        dag_id=f"sync_{connector}_{group}",
        schedule=schedule,
        catchup=False,
        max_active_runs=1,
        default_args={"retries": 2, "retry_delay": retry_delay},
        tags=["vecten", "sync", connector],
    )
    def sync_dag():
        @task(task_id="discover", on_failure_callback=alert_on_failure)
        def discover() -> list[str]:
            # The mapped sync tasks run in parallel, so the schemas they share exist first.
            ensure_schemas(connector)
            return discover_objects(connector, objects)

        @task(
            task_id="sync",
            retries=2,
            on_failure_callback=alert_on_failure,
            map_index_template="{{ object_name }}",
        )
        def sync(object_name: str) -> dict:
            # Airflow renders the template from the context after the task, failed or not.
            get_current_context()["object_name"] = object_name
            return sync_one(connector, object_name)

        @task(task_id="publish", outlets=[asset_for(connector, group)], trigger_rule="all_done")
        def publish() -> None:
            return None

        # The run's state follows its leaf; publish emits the asset whatever happened, so this
        # leaf is what marks the run failed when an object failed.
        @task(task_id="check", trigger_rule="all_done", retries=0)
        def check(discovered: list[str]) -> None:
            ti = get_current_context()["ti"]
            states = ti.get_task_states(dag_id=ti.dag_id, task_ids=["sync"], run_ids=[ti.run_id])
            raise_for_failed(connector, discovered, states.get(ti.run_id, {}))

        discovered = discover()
        sync.expand(object_name=discovered) >> publish() >> check(discovered)

    return sync_dag()
