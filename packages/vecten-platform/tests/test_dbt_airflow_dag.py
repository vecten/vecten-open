from datetime import timedelta

import pytest

airflow = pytest.importorskip("airflow")

from vecten_platform.dbt.airflow_dag import build_dbt_dag, built_asset, dbt_assets  # noqa: E402
from vecten_shared.config import VectenConfig  # noqa: E402

TASKS = {"deps", "build", "convention", "freshness", "mark_built", "publish"}


def test_dag_structure_and_schedule():
    from airflow.sdk.definitions.asset import AssetAll

    dag = build_dbt_dag([("hubspot", "default"), ("stripe", "daily")])
    assert dag.dag_id == "dbt_build" and dag.max_active_runs == 1
    assert dag.catchup is False and sorted(dag.tags) == ["dbt", "vecten"]
    assert {t.task_id for t in dag.tasks} == TASKS
    assert dag.get_task("build").upstream_task_ids == {"deps"}
    assert dag.get_task("convention").upstream_task_ids == {"build"}
    assert dag.get_task("freshness").upstream_task_ids == {"deps"}
    assert dag.get_task("mark_built").upstream_task_ids == {"convention"}
    condition = dag.timetable.asset_condition
    assert isinstance(condition, AssetAll)
    names = sorted(asset.name for asset in condition.objects)
    assert names == ["raw_hubspot.default", "raw_stripe.daily"]


def test_a_green_build_publishes_the_built_asset():
    dag = build_dbt_dag([("hubspot", "default")])
    publish = dag.get_task("publish")
    assert publish.upstream_task_ids == {"convention"}
    assert publish.trigger_rule == "all_success"
    assert [a.name for a in publish.outlets] == ["dbt_build"]
    assert publish.outlets[0].uri == built_asset().uri == "vecten://dbt/build"


def test_every_task_alerts_and_retries_after_the_delay():
    dag = build_dbt_dag([("hubspot", "default")])
    for task_id in TASKS:
        task = dag.get_task(task_id)
        assert task.on_failure_callback is not None, task_id
        assert task.retry_delay == timedelta(minutes=10), task_id
    quick = build_dbt_dag([], retry_delay=timedelta(seconds=1))
    assert quick.get_task("build").retry_delay == timedelta(seconds=1)


def test_no_connectors_means_manual_schedule():
    assert build_dbt_dag([]).timetable.can_be_scheduled is False


def test_any_trigger_builds_when_one_group_has_updated():
    from airflow.sdk.definitions.asset import AssetAny

    dag = build_dbt_dag([("hubspot", "hourly"), ("stripe", "daily")], trigger="any")
    condition = dag.timetable.asset_condition
    assert isinstance(condition, AssetAny)
    assert sorted(a.name for a in condition.objects) == ["raw_hubspot.hourly", "raw_stripe.daily"]
    with pytest.raises(ValueError, match="trigger is one of all, any"):
        build_dbt_dag([("hubspot", "hourly")], trigger="some")


def test_run_parameters_default_to_the_full_build():
    dag = build_dbt_dag([("hubspot", "default")])
    assert dag.params.dump() == {"select": "", "exclude": "", "full_refresh": False}
    assert dag.is_paused_upon_creation is None


def test_the_dag_file_sets_the_run_parameter_defaults():
    dag = build_dbt_dag(
        [("hubspot", "default")],
        select=["tag:intraday", "core"],
        exclude="marts",
        full_refresh=True,
    )
    assert dag.params.dump() == {
        "select": "tag:intraday core",
        "exclude": "marts",
        "full_refresh": True,
    }
    with pytest.raises(ValueError, match="does not start with '-'"):
        build_dbt_dag([], select="--vars {}")


def test_paused_at_creation_overrides_the_environment_default():
    assert build_dbt_dag([], paused_at_creation=True).is_paused_upon_creation is True
    assert build_dbt_dag([], paused_at_creation=False).is_paused_upon_creation is False


def test_assets_from_config():
    cfg = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "a", "scaffold_version": "0.1.0"},
            "connectors": [
                {
                    "name": "hubspot",
                    "source": "dlt",
                    "groups": {"default": [], "crm": ["companies"]},
                }
            ],
        }
    )
    assert dbt_assets(cfg) == [("hubspot", "crm"), ("hubspot", "default")]


def serialize_to_db(dag) -> None:
    # dag.test() finds a DAG only in the metadata database, which parsing the dags folder fills.
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.dag_processing.dagbag import DagBag, sync_bag_to_db

    DagBundlesManager().sync_bundles_to_db()
    bag = DagBag(collect_dags=False)
    bag.bag_dag(dag)
    sync_bag_to_db(bag, "dags-folder", None)


def run_dbt_dag(monkeypatch, run_conf=None, convention=None, **options):
    """Run the dbt DAG with dag.test() and its dbt steps replaced; return the run, the task
    states, the build calls and the extra of each `dbt_build` asset event."""
    from airflow.models.asset import AssetEvent
    from airflow.utils.session import create_session
    from sqlalchemy import select

    from vecten_platform.dbt import airflow_dag

    calls = {"build": [], "mark_built": 0}
    monkeypatch.setattr(airflow_dag, "install_deps", lambda: None)
    monkeypatch.setattr(airflow_dag, "convention_step", convention or (lambda run_id: None))
    monkeypatch.setattr(airflow_dag, "freshness_check", lambda run_id: None)

    def build(run_id, select=(), exclude=(), *, full_refresh=False):
        calls["build"].append((select, exclude, full_refresh))

    def mark():
        calls["mark_built"] += 1

    monkeypatch.setattr(airflow_dag, "build_step", build)
    monkeypatch.setattr(airflow_dag, "mark_built", mark)
    # dag.test() waits out every retry delay in real time.
    dag = build_dbt_dag([("hubspot", "default")], retry_delay=timedelta(0), **options)
    serialize_to_db(dag)
    run = dag.test(run_conf=run_conf)
    states = {ti.task_id: ti.state for ti in run.get_task_instances()}
    with create_session() as session:
        extras = session.scalars(
            select(AssetEvent.extra).where(
                AssetEvent.source_dag_id == run.dag_id, AssetEvent.source_run_id == run.run_id
            )
        ).all()
    return run, states, calls, extras


@pytest.mark.docker
def test_dag_test_a_full_build_marks_the_commit_built_and_publishes(monkeypatch):
    run, states, calls, extras = run_dbt_dag(monkeypatch)
    assert run.state == "success"
    assert calls == {"build": [("", "", False)], "mark_built": 1}
    assert states["mark_built"] == "success" and states["publish"] == "success"
    assert extras == [{"select": "", "exclude": "", "full_refresh": False}]


@pytest.mark.docker
def test_dag_test_a_narrowed_run_builds_its_selection_and_leaves_the_commit(monkeypatch):
    conf = {"select": "tag:intraday", "full_refresh": True}
    run, states, calls, extras = run_dbt_dag(monkeypatch, run_conf=conf)
    assert run.state == "success"
    assert calls == {"build": [("tag:intraday", "", True)], "mark_built": 0}
    assert states["mark_built"] == "skipped" and states["publish"] == "success"
    assert extras == [{"select": "tag:intraday", "exclude": "", "full_refresh": True}]


@pytest.mark.docker
def test_dag_test_a_failed_build_publishes_nothing(monkeypatch):
    def failing(run_id):
        raise RuntimeError("dbt test failed: unique_company_id")

    run, states, calls, extras = run_dbt_dag(monkeypatch, convention=failing)
    assert run.state == "failed"
    assert states["convention"] == "failed"
    assert states["publish"] == "upstream_failed" and states["mark_built"] == "upstream_failed"
    assert calls["mark_built"] == 0
    assert extras == []
