"""The sync DAG: discover objects at run time, one mapped task per object, one asset per group.

Objects run in parallel unless the DAG file orders them in stages: each stage is then its own
mapped task, started once every object of the stage before it has synced.

The loader re-imports `connectors.<name>` on every call, so a process must hold one
connector of a given name: one DAG file per dag-processor subprocess and one connector per
task process keep two projects' same-named connectors from resolving against each other.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from vecten_platform.sync.alerts import alert_on_failure
from vecten_platform.sync.context import runtime
from vecten_platform.sync.destination import warehouse_destination
from vecten_platform.sync.loader import load_connector
from vecten_platform.sync.runner import ensure_datasets, list_objects, run_object
from vecten_shared.issues import SYNC_TASK, sync_stage_task
from vecten_shared.output import Failure

ASSET_SCHEME = "vecten"
_PASSED = {"success", "skipped"}
_NOT_RUN = "upstream_failed"


def discover_objects(
    connector: str, objects: list[str], params: Mapping[str, Any] | None = None
) -> list[str]:
    if objects:
        return list(objects)
    ctx = runtime()
    module = load_connector(ctx.connectors_dir, connector)
    return list_objects(module.source(**(params or {})))


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


def sync_one(connector: str, object_name: str, params: Mapping[str, Any] | None = None) -> dict:
    try:
        ctx = runtime()
        module = load_connector(ctx.connectors_dir, connector)
        make_source = functools.partial(module.source, **params) if params else module.source
        outcome = run_object(
            connector, make_source, object_name, warehouse_destination(ctx), ctx.dlt_dir
        )
    except Exception as exc:
        # A missing secret or a connector that does not import fails one object's task too,
        # and the alert and `vecten issues` key every such failure by its object.
        raise ObjectSyncError(connector, object_name, _describe(exc)) from exc
    if not outcome.ok:
        raise ObjectSyncError(connector, object_name, outcome.error or "failed")
    return {"object": object_name, "rows": outcome.rows}


def checked_stages(
    objects: Sequence[str], stages: Sequence[Sequence[str]] | None
) -> list[list[str]] | None:
    """The stages as lists, refused at parse time when they cannot drive the DAG."""
    if stages is None:
        return None
    # A bare string would otherwise split into one object per character.
    if isinstance(stages, str) or any(isinstance(stage, str) for stage in stages):
        raise ValueError("stages is a list of lists of object names, one list per stage")
    staged = [list(stage) for stage in stages]
    if not staged or not all(staged):
        raise ValueError("stages needs at least one stage, and every stage at least one object")
    names = [name for stage in staged for name in stage]
    if not all(isinstance(name, str) for name in names):
        raise ValueError("stages holds object names, which are strings")
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        raise ValueError(f"stages names {', '.join(repeated)} more than once")
    if objects and sorted(objects) != sorted(names):
        raise ValueError("objects and stages name different objects; leave objects empty")
    return staged


def object_tasks(
    objects: Sequence[str], stages: Sequence[Sequence[str]] | None
) -> list[tuple[str, list[str]]]:
    """Each per-object task id with the objects it maps, map index i being its i-th object."""
    if not stages:
        return [(SYNC_TASK, list(objects))]
    return [(sync_stage_task(n), list(stage)) for n, stage in enumerate(stages, start=1)]


def failed_objects(
    objects: list[str], states: dict[str, str | None], task_id: str = SYNC_TASK
) -> list[str]:
    """`states` is keyed `<task_id>_<map_index>`, and map index i is the task's i-th object."""
    return [name for i, name in enumerate(objects) if states.get(f"{task_id}_{i}") not in _PASSED]


def raise_for_failed(
    connector: str,
    objects: list[str],
    states: dict[str, str | None],
    stages: Sequence[Sequence[str]] | None = None,
) -> None:
    failed: list[str] = []
    not_run: list[str] = []
    layout = object_tasks(objects, stages)
    for task_id, names in layout:
        for name in failed_objects(names, states, task_id):
            # A stage after a failed one may never expand, leaving one state for the task.
            index = names.index(name)
            state = states.get(f"{task_id}_{index}", states.get(task_id))
            (not_run if state == _NOT_RUN else failed).append(name)
    if not failed and not not_run:
        return
    total = sum(len(names) for _, names in layout)
    parts = [f"{len(failed)} of {total} objects failed: {', '.join(failed)}"] if failed else []
    if not_run:
        parts.append(f"{len(not_run)} not run after an earlier stage failed: {', '.join(not_run)}")
    raise RuntimeError(f"{connector}: {'; '.join(parts)}")


def asset_for(connector: str, group: str):
    from airflow.sdk import Asset

    return Asset(name=f"raw_{connector}.{group}", uri=f"{ASSET_SCHEME}://raw_{connector}/{group}")


def sync_timetable(schedule: object, timezone: str | None):
    """A cron string as a timetable in `timezone` (UTC when None); any other schedule as given."""
    from airflow.sdk import CronDataIntervalTimetable

    if not isinstance(schedule, str):
        if timezone is not None:
            raise ValueError("timezone applies to a cron string; give a timetable its own")
        return schedule
    zone = timezone or "UTC"
    try:
        ZoneInfo(zone)
    except ValueError, LookupError:
        raise ValueError(
            f"unknown timezone {zone!r}; use an IANA name such as Europe/Warsaw"
        ) from None
    # A plain cron string first runs at the next boundary; without catchup, this one runs
    # the latest whole interval once the DAG is unpaused, so a fresh deploy syncs at once.
    return CronDataIntervalTimetable(schedule, timezone=zone)


def build_sync_dag(
    connector: str,
    group: str,
    objects: list[str],
    schedule: object = "@daily",
    retry_delay: timedelta = timedelta(minutes=5),  # noqa: B008
    *,
    stages: Sequence[Sequence[str]] | None = None,
    params: Mapping[str, Any] | None = None,
    pool: str | None = None,
    guard: Callable[[], bool] | None = None,
    timezone: str | None = None,
    paused_at_creation: bool | None = None,
):
    """The sync DAG of one object group; every keyword left out keeps the default behaviour.

    `stages` orders the objects: a list of lists of names, each stage starting once every
    object of the one before it synced. `params` are the DAG's run parameters, each passed to
    the connector's `source()` as a keyword argument. `pool` is an Airflow pool the object
    tasks take a slot of. `guard` runs first, and a falsy return skips the whole run. `timezone`
    is the IANA zone a cron string `schedule` is read in. `paused_at_creation` overrides the
    environment's default for whether the DAG starts paused.
    """
    # Airflow lives only in the Airflow image; importing it here keeps `connector run` light.
    from airflow.sdk import dag as dag_decorator
    from airflow.sdk import get_current_context, task

    staged = checked_stages(objects, stages)
    listed = [name for stage in staged for name in stage] if staged else list(objects)
    timetable = sync_timetable(schedule, timezone)
    declared = dict(params or {})
    if guard is not None and not callable(guard):
        raise ValueError("guard is a function returning whether the run goes ahead")
    overrides = {"pool": pool} if pool else {}

    def source_params() -> dict[str, Any]:
        # A run's conf can carry keys the DAG never declared; only declared params reach source().
        if not declared:
            return {}
        given = get_current_context()["params"]
        return {name: given[name] for name in declared}

    @dag_decorator(
        dag_id=f"sync_{connector}_{group}",
        schedule=timetable,
        catchup=False,
        max_active_runs=1,
        default_args={"retries": 2, "retry_delay": retry_delay},
        tags=["vecten", "sync", connector],
        params=declared,
        is_paused_upon_creation=paused_at_creation,
    )
    def sync_dag():
        @task(task_id="discover", on_failure_callback=alert_on_failure)
        def discover() -> list[str]:
            # The mapped sync tasks run in parallel, so the schemas they share exist first.
            ensure_schemas(connector)
            return discover_objects(connector, listed, source_params())

        @task(
            task_id=SYNC_TASK,
            retries=2,
            on_failure_callback=alert_on_failure,
            map_index_template="{{ object_name }}",
        )
        def sync(object_name: str) -> dict:
            # Airflow renders the template from the context after the task, failed or not.
            get_current_context()["object_name"] = object_name
            return sync_one(connector, object_name, source_params())

        # all_done: a failed object must still reach this task, which names it and fails the run.
        @task(task_id="check", trigger_rule="all_done", retries=0)
        def check(discovered: list[str]) -> None:
            ti = get_current_context()["ti"]
            task_ids = [task_id for task_id, _ in object_tasks(discovered, staged)]
            states = ti.get_task_states(dag_id=ti.dag_id, task_ids=task_ids, run_ids=[ti.run_id])
            raise_for_failed(connector, discovered, states.get(ti.run_id, {}), staged)

        # The dbt DAG schedules on this asset, so it is emitted only once check has found every
        # object synced; after a failed object dbt would build on partial data.
        @task(task_id="publish", outlets=[asset_for(connector, group)])
        def publish() -> None:
            return None

        discovered = discover()
        if staged is None:
            synced = [sync.override(**overrides).expand(object_name=discovered)]
        else:
            synced = [
                sync.override(task_id=task_id, **overrides).expand(object_name=names)
                for task_id, names in object_tasks(listed, staged)
            ]
            discovered >> synced[0]
            for earlier, later in pairwise(synced):
                earlier >> later
        # check waits on every stage: a stage after a failed one is done at once, while
        # objects of the failed stage may still be running.
        synced >> check(discovered) >> publish()

        if guard is not None:

            @task.short_circuit(task_id="guard", on_failure_callback=alert_on_failure)
            def run_guard() -> bool:
                return bool(guard())

            # short_circuit skips every downstream task whatever its trigger rule, so a guarded
            # run ends with nothing synced, nothing published and no failure.
            run_guard() >> discovered

    return sync_dag()
