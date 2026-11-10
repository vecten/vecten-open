from datetime import timedelta

import pytest

airflow = pytest.importorskip("airflow")

from vecten_platform.dbt.airflow_dag import build_dbt_dag, dbt_assets  # noqa: E402
from vecten_shared.config import VectenConfig  # noqa: E402

TASKS = {"deps", "build", "convention", "freshness", "mark_built"}


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
