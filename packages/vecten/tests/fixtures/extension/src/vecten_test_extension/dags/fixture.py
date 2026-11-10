from pathlib import Path

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, dag, task


def record_execution():
    Path("/tmp/vecten-extension-task-ran").write_text("executed")


with DAG("fixture_dag", schedule=None) as fixture:
    extract = PythonOperator(task_id="extract", python_callable=record_execution)


@dag(dag_id="fixture_decorated", schedule=None)
def pipeline():
    @task
    def extract():
        return "decorated"

    extract()


pipeline()
