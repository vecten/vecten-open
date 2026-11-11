from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

airflow = pytest.importorskip("airflow")

from vecten_platform.sync import context  # noqa: E402
from vecten_platform.sync.airflow_dag import asset_for, build_sync_dag  # noqa: E402
from vecten_shared.provider.local import LocalProvider  # noqa: E402

FIXTURES = Path(__file__).parent / "fixture_connector"


def test_dag_shape():
    dag = build_sync_dag("hubspot", "default", [], schedule="@daily")
    assert dag.dag_id == "sync_hubspot_default"
    assert dag.max_active_runs == 1 and dag.catchup is False
    from airflow.sdk.definitions.timetables.interval import CronDataIntervalTimetable

    assert isinstance(dag.timetable, CronDataIntervalTimetable)
    assert set(dag.task_ids) == {"discover", "sync", "publish", "check"}
    check = dag.get_task("check")
    assert check.trigger_rule == "all_done"
    assert check.retries == 0
    assert "sync" in check.upstream_task_ids
    publish = dag.get_task("publish")
    assert [a.name for a in publish.outlets] == [asset_for("hubspot", "default").name]
    sync = dag.get_task("sync")
    assert sync.retries == 2
    assert sync.on_failure_callback is not None
    # The rendered map index names the object, which is how `vecten issues` groups attempts.
    assert sync.map_index_template == "{{ object_name }}"
    assert dag.get_task("discover").on_failure_callback is not None


def test_the_asset_is_published_only_after_every_object_synced():
    # dbt schedules on the asset, so publishing it after a failed object builds on partial data.
    dag = build_sync_dag("hubspot", "default", [])
    publish = dag.get_task("publish")
    assert publish.upstream_task_ids == {"check"}
    assert publish.trigger_rule == "all_success"
    assert dag.get_task("check").downstream_task_ids == {"publish"}


def test_first_run_is_due_when_the_dag_appears():
    from airflow.serialization.encoders import coerce_to_core_timetable
    from airflow.timetables.base import TimeRestriction

    dag = build_sync_dag("hubspot", "default", [], schedule="@daily")
    now = datetime.now(UTC)
    # The scheduler plans runs with the core timetable the DAG's definition serializes to.
    info = coerce_to_core_timetable(dag.timetable).next_dagrun_info(
        last_automated_data_interval=None,
        restriction=TimeRestriction(earliest=None, latest=None, catchup=False),
    )
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert info.run_after <= now
    assert info.data_interval.end == midnight
    assert info.data_interval.start == midnight - timedelta(days=1)


def test_sync_retries_wait_five_minutes_unless_the_dag_file_says_otherwise():
    assert build_sync_dag("hubspot", "default", []).get_task("sync").retry_delay == timedelta(
        minutes=5
    )
    quick = build_sync_dag("hubspot", "default", [], retry_delay=timedelta(seconds=1))
    assert quick.get_task("sync").retry_delay == timedelta(seconds=1)
    assert quick.get_task("sync").retries == 2


def test_dag_with_explicit_objects_skips_discovery_call(monkeypatch):
    from vecten_platform.sync import airflow_dag

    monkeypatch.setattr(airflow_dag, "ensure_schemas", lambda connector: None)
    dag = build_sync_dag("hubspot", "crm", ["companies", "deals"])
    discover = dag.get_task("discover")
    assert discover.python_callable() == ["companies", "deals"]


def test_discover_ensures_the_schemas_before_listing(monkeypatch):
    from vecten_platform.sync import airflow_dag

    order = []
    monkeypatch.setattr(airflow_dag, "ensure_schemas", lambda connector: order.append("ensure"))
    monkeypatch.setattr(
        airflow_dag, "discover_objects", lambda *args: order.append("discover") or []
    )
    build_sync_dag("hubspot", "crm", []).get_task("discover").python_callable()
    assert order == ["ensure", "discover"]


def test_options_left_out_keep_the_default_dag():
    dag = build_sync_dag("hubspot", "default", [])
    assert dict(dag.params) == {}
    assert dag.is_paused_upon_creation is None
    assert dag.timetable.timezone == "UTC"
    assert dag.get_task("sync").pool == "default_pool"
    assert dag.get_task("discover").upstream_task_ids == set()


def test_stages_run_one_after_another():
    dag = build_sync_dag("hubspot", "crm", [], stages=[["companies"], ["contacts", "deals"]])
    assert set(dag.task_ids) == {"discover", "sync_stage_1", "sync_stage_2", "check", "publish"}
    assert dag.get_task("sync_stage_1").upstream_task_ids == {"discover"}
    assert dag.get_task("sync_stage_2").upstream_task_ids == {"sync_stage_1"}
    # check must wait for every stage, not only the last, which ends early after a failure.
    assert {"sync_stage_1", "sync_stage_2"} <= dag.get_task("check").upstream_task_ids
    for task_id in ("sync_stage_1", "sync_stage_2"):
        task = dag.get_task(task_id)
        assert task.map_index_template == "{{ object_name }}"
        assert task.retries == 2 and task.on_failure_callback is not None


def test_staged_discover_lists_the_staged_objects_without_the_source(monkeypatch):
    from vecten_platform.sync import airflow_dag

    monkeypatch.setattr(airflow_dag, "ensure_schemas", lambda connector: None)
    dag = build_sync_dag("hubspot", "crm", [], stages=[["companies"], ["contacts"]])
    assert dag.get_task("discover").python_callable() == ["companies", "contacts"]


def test_params_become_run_parameters():
    from airflow.sdk import Param

    dag = build_sync_dag(
        "hubspot", "default", [], params={"since": Param(None, type=["null", "string"]), "cap": 50}
    )
    assert set(dag.params) == {"since", "cap"}
    assert dag.params["cap"] == 50


def test_pool_applies_to_the_object_tasks_only():
    dag = build_sync_dag("hubspot", "default", [], pool="hubspot_api")
    assert dag.get_task("sync").pool == "hubspot_api"
    assert dag.get_task("discover").pool == "default_pool"
    staged = build_sync_dag("hubspot", "crm", [], stages=[["a"], ["b"]], pool="hubspot_api")
    assert {staged.get_task(t).pool for t in ("sync_stage_1", "sync_stage_2")} == {"hubspot_api"}


def test_guard_runs_before_anything_else():
    from airflow.providers.standard.operators.python import ShortCircuitOperator

    dag = build_sync_dag("hubspot", "default", [], guard=lambda: True)
    guard = dag.get_task("guard")
    assert isinstance(guard, ShortCircuitOperator)
    assert guard.ignore_downstream_trigger_rules is True
    assert guard.upstream_task_ids == set()
    assert dag.get_task("discover").upstream_task_ids == {"guard"}
    with pytest.raises(ValueError, match="guard"):
        build_sync_dag("hubspot", "default", [], guard="halted")


def test_timezone_reads_the_cron_string_in_local_time():
    from zoneinfo import ZoneInfo

    from airflow.serialization.encoders import coerce_to_core_timetable
    from airflow.timetables.base import TimeRestriction

    dag = build_sync_dag("hubspot", "default", [], schedule="0 6 * * *", timezone="Europe/Warsaw")
    assert dag.timetable.timezone == "Europe/Warsaw"
    info = coerce_to_core_timetable(dag.timetable).next_dagrun_info(
        last_automated_data_interval=None,
        restriction=TimeRestriction(earliest=None, latest=None, catchup=False),
    )
    local_end = info.data_interval.end.astimezone(ZoneInfo("Europe/Warsaw"))
    assert (local_end.hour, local_end.minute) == (6, 0)
    assert info.run_after <= datetime.now(UTC)


def test_timezone_is_refused_where_it_cannot_apply():
    from airflow.sdk import CronTriggerTimetable

    with pytest.raises(ValueError, match="unknown timezone"):
        build_sync_dag("hubspot", "default", [], timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="cron string"):
        build_sync_dag(
            "hubspot",
            "default",
            [],
            schedule=CronTriggerTimetable("@daily", timezone="UTC"),
            timezone="Europe/Warsaw",
        )


def test_a_timetable_schedule_is_used_as_given():
    from airflow.sdk import CronTriggerTimetable

    timetable = CronTriggerTimetable("0 6 * * *", timezone="Europe/Warsaw")
    assert build_sync_dag("hubspot", "default", [], schedule=timetable).timetable is timetable


def test_paused_at_creation_overrides_the_environment_default():
    assert build_sync_dag("hubspot", "default", [], paused_at_creation=True).is_paused_upon_creation
    unpaused = build_sync_dag("hubspot", "default", [], paused_at_creation=False)
    assert unpaused.is_paused_upon_creation is False


def serialize_to_db(dag) -> None:
    # dag.test() refuses a DAG whose serialized version is not in the metadata database, and
    # finds it only by parsing the dags folder; this DAG lives in a test, so it is written directly.
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.dag_processing.dagbag import DagBag, sync_bag_to_db

    DagBundlesManager().sync_bundles_to_db()
    bag = DagBag(collect_dags=False)
    bag.bag_dag(dag)
    sync_bag_to_db(bag, "dags-folder", None)


def run_demo_dag(tmp_path, monkeypatch, objects, run_conf=None, **options):
    """Run the demo connector's DAG with dag.test(); return the run and every check verdict."""
    import dlt

    from vecten_platform.sync import airflow_dag

    provider = LocalProvider(tmp_path, inside_compose=False)
    provider.put_secret("DEMO_TOKEN", "t")
    provider.put_secret("LOADER_PASSWORD", "unused")
    ctx = context.RuntimeContext(tmp_path, FIXTURES, "local", provider, tmp_path / "dlt")
    monkeypatch.setattr(context, "_RUNTIME", ctx)
    dest = dlt.destinations.duckdb(str(tmp_path / "wh.duckdb"))
    monkeypatch.setattr(airflow_dag, "warehouse_destination", lambda ctx: dest)
    verdicts = []
    real_failed_objects = airflow_dag.failed_objects

    def spy(names, states, *task_id):
        verdicts.append(real_failed_objects(names, states, *task_id))
        return verdicts[-1]

    monkeypatch.setattr(airflow_dag, "failed_objects", spy)
    dag = build_sync_dag("demo", "default", objects, **options)
    # dag.test() waits out every retry delay in real time.
    for task in dag.tasks:
        task.retry_delay = timedelta(0)
    serialize_to_db(dag)
    run = dag.test(run_conf=run_conf)
    states = {
        ti.task_id + (f"[{ti.rendered_map_index}]" if ti.map_index >= 0 else ""): ti.state
        for ti in run.get_task_instances()
    }
    return run, states, verdicts


def asset_events(run) -> int:
    from airflow.models.asset import AssetEvent
    from airflow.utils.session import create_session
    from sqlalchemy import func, select

    with create_session() as session:
        return session.scalar(
            select(func.count())
            .select_from(AssetEvent)
            .where(AssetEvent.source_dag_id == run.dag_id, AssetEvent.source_run_id == run.run_id)
        )


@pytest.mark.docker
def test_dag_test_all_objects_succeed(tmp_path, monkeypatch):
    run, states, verdicts = run_demo_dag(tmp_path, monkeypatch, ["good_a", "good_b"])
    assert run.state == "success"
    assert states["check"] == "success" and states["publish"] == "success"
    assert verdicts == [[]]
    assert asset_events(run) == 1


@pytest.mark.docker
def test_dag_test_one_object_fails_others_keep_checkpoints(tmp_path, monkeypatch):
    import duckdb

    run, states, verdicts = run_demo_dag(tmp_path, monkeypatch, [])
    assert run.state == "failed"
    con = duckdb.connect(str(tmp_path / "wh.duckdb"), read_only=True)
    try:
        assert con.execute("select count(*) from raw_demo.good_a").fetchone()[0] == 2
        assert con.execute("select count(*) from raw_demo.good_b").fetchone()[0] == 1
        names = {
            r[0]
            for r in con.execute(
                "select pipeline_name from raw_demo._dlt_pipeline_state"
            ).fetchall()
        }
    finally:
        con.close()
    assert names == {"demo__good_a", "demo__good_b"}
    assert states["check"] == "failed"
    assert states["publish"] == "upstream_failed"
    assert asset_events(run) == 0
    assert verdicts == [["bad"]]
    assert {k: v for k, v in states.items() if k.startswith("sync[")} == {
        "sync[bad]": "failed",
        "sync[good_a]": "success",
        "sync[good_b]": "success",
    }


def warehouse_count(tmp_path, table: str) -> int | None:
    import duckdb

    con = duckdb.connect(str(tmp_path / "wh.duckdb"), read_only=True)
    try:
        return con.execute(f"select count(*) from raw_demo.{table}").fetchone()[0]
    except duckdb.CatalogException:
        return None
    finally:
        con.close()


@pytest.mark.docker
def test_dag_test_a_false_guard_skips_the_run_cleanly(tmp_path, monkeypatch):
    run, states, verdicts = run_demo_dag(tmp_path, monkeypatch, ["good_a"], guard=lambda: False)
    assert run.state == "success"
    assert states.pop("guard") == "success"
    assert set(states.values()) == {"skipped"}
    assert {"discover", "check", "publish"} <= set(states)
    assert verdicts == []
    assert asset_events(run) == 0


@pytest.mark.docker
def test_dag_test_a_true_guard_lets_the_run_go_ahead(tmp_path, monkeypatch):
    run, states, _ = run_demo_dag(tmp_path, monkeypatch, ["good_b"], guard=lambda: True)
    assert run.state == "success" and states["publish"] == "success"
    assert warehouse_count(tmp_path, "good_b") == 1


@pytest.mark.docker
def test_dag_test_params_reach_the_source(tmp_path, monkeypatch):
    from airflow.sdk import Param

    params = {"limit": Param(None, type=["null", "integer"])}
    run, _, _ = run_demo_dag(
        tmp_path, monkeypatch, ["good_a"], run_conf={"limit": 1, "undeclared": 1}, params=params
    )
    assert run.state == "success"
    assert warehouse_count(tmp_path, "good_a") == 1


@pytest.mark.docker
def test_dag_test_params_default_on_a_run_without_conf(tmp_path, monkeypatch):
    from airflow.sdk import Param

    params = {"limit": Param(None, type=["null", "integer"])}
    run, _, _ = run_demo_dag(tmp_path, monkeypatch, ["good_a"], params=params)
    assert run.state == "success"
    assert warehouse_count(tmp_path, "good_a") == 2


@pytest.mark.docker
def test_dag_test_stages_run_in_order(tmp_path, monkeypatch):
    run, states, verdicts = run_demo_dag(tmp_path, monkeypatch, [], stages=[["good_a"], ["good_b"]])
    assert run.state == "success" and states["publish"] == "success"
    tis = {ti.task_id: ti for ti in run.get_task_instances() if ti.map_index >= 0}
    assert tis["sync_stage_2"].start_date >= tis["sync_stage_1"].end_date
    assert verdicts == [[], []]
    assert asset_events(run) == 1


@pytest.mark.docker
def test_dag_test_a_failed_stage_stops_the_later_ones(tmp_path, monkeypatch):
    from vecten_platform.sync import airflow_dag

    messages = []
    real_raise = airflow_dag.raise_for_failed

    def spy(*args):
        try:
            real_raise(*args)
        except RuntimeError as exc:
            messages.append(str(exc))
            raise

    monkeypatch.setattr(airflow_dag, "raise_for_failed", spy)
    run, states, _ = run_demo_dag(tmp_path, monkeypatch, [], stages=[["bad"], ["good_a"]])
    assert run.state == "failed"
    assert states["sync_stage_1[bad]"] == "failed"
    assert not any(k.startswith("sync_stage_2") and v == "success" for k, v in states.items())
    assert states["check"] == "failed" and states["publish"] == "upstream_failed"
    assert messages == [
        "demo: 1 of 2 objects failed: bad; 1 not run after an earlier stage failed: good_a"
    ]
    assert warehouse_count(tmp_path, "good_a") is None
    assert asset_events(run) == 0
