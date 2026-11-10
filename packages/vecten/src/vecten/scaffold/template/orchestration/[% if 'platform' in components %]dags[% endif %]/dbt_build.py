"""The dbt build DAG: runs from the shipped bundle once every sync asset has updated."""

from pathlib import Path

from vecten_platform.dbt.airflow_dag import build_dbt_dag, dbt_assets
from vecten_platform.sync.context import runtime
from vecten_shared.config import load_config

dag = build_dbt_dag(dbt_assets(load_config(Path(runtime().project_root))))
