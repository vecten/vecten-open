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
    publish = dag.get_task("publish")
    assert publish.trigger_rule == "all_done"
    check = dag.get_task("check")
    assert check.trigger_rule == "all_done"
    assert check.retries == 0
    assert "publish" in check.upstream_task_ids
    assert [a.name for a in publish.outlets] == [asset_for("hubspot", "default").name]
    sync = dag.get_task("sync")
    assert sync.retries == 2
    assert sync.on_failure_callback is not None
    # The rendered map index names the object, which is how `vecten issues` groups attempts.
    assert sync.map_index_template == "{{ object_name }}"
    assert dag.get_task("discover").on_failure_callback is not None


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
        airflow_dag, "discover_objects", lambda connector, objects: order.append("discover") or []
    )
    build_sync_dag("hubspot", "crm", []).get_task("discover").python_callable()
    assert order == ["ensure", "discover"]


def serialize_to_db(dag) -> None:
    # dag.test() refuses a DAG whose serialized version is not in the metadata database, and
    # finds it only by parsing the dags folder; this DAG lives in a test, so it is written directly.
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.dag_processing.dagbag import DagBag, sync_bag_to_db

    DagBundlesManager().sync_bundles_to_db()
    bag = DagBag(collect_dags=False)
    bag.bag_dag(dag)
    sync_bag_to_db(bag, "dags-folder", None)


def run_demo_dag(tmp_path, monkeypatch, objects):
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

    def spy(names, states):
        verdicts.append(real_failed_objects(names, states))
        return verdicts[-1]

    monkeypatch.setattr(airflow_dag, "failed_objects", spy)
    dag = build_sync_dag("demo", "default", objects)
    # dag.test() waits out every retry delay in real time.
    dag.get_task("sync").retry_delay = timedelta(0)
    serialize_to_db(dag)
    run = dag.test()
    states = {
        ti.task_id + (f"[{ti.rendered_map_index}]" if ti.map_index >= 0 else ""): ti.state
        for ti in run.get_task_instances()
    }
    return run, states, verdicts


@pytest.mark.docker
def test_dag_test_all_objects_succeed(tmp_path, monkeypatch):
    run, states, verdicts = run_demo_dag(tmp_path, monkeypatch, ["good_a", "good_b"])
    assert run.state == "success"
    assert states["check"] == "success" and states["publish"] == "success"
    assert verdicts == [[]]


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
    assert states["publish"] == "success"
    assert states["check"] == "failed"
    assert verdicts == [["bad"]]
    assert {k: v for k, v in states.items() if k.startswith("sync[")} == {
        "sync[bad]": "failed",
        "sync[good_a]": "success",
        "sync[good_b]": "success",
    }
