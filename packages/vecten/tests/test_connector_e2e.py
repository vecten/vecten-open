"""init, connector add, up, run --record, raw schema, replay, the DAG in Airflow, down."""

import json
import os
import subprocess
import sys
import time
import tomllib
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import httpx
import psycopg
import pytest
from dotenv import dotenv_values

from vecten.stack.compose import LOCAL_HTTPS_PORT, compose_project_name
from vecten_shared.config import load_config, save_config
from vecten_shared.provider.local import local_pg_published_port

from .docker_util import remove_stack_images

pytestmark = pytest.mark.docker
REPO = Path(__file__).resolve().parents[3]
AIRFLOW_HOST = f"airflow.localhost:{LOCAL_HTTPS_PORT}"
DAG_ID = "sync_demo_default"
FIXTURE = (
    REPO / "packages" / "vecten-platform" / "tests" / "fixture_connector" / "demo" / "connector.py"
)


def docker_ok() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def vecten(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def drop_volumes(project: str) -> None:
    names = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
        capture_output=True,
        text=True,
    ).stdout.split()
    if names:
        subprocess.run(["docker", "volume", "rm", *names], capture_output=True, check=True)


class AirflowApi:
    """Airflow's REST API through the NGINX front door.

    NGINX takes basic auth in the Authorization header, so Airflow's JWT rides in the `_token`
    cookie, which Airflow reads when the Authorization header carries no Bearer token.
    """

    def __init__(self, client: httpx.Client):
        self.client = client

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self.client.request(
            method,
            f"https://127.0.0.1:{LOCAL_HTTPS_PORT}{path}",
            extensions={"sni_hostname": AIRFLOW_HOST.split(":", 1)[0]},
            **kwargs,
        )

    def get(self, path: str) -> httpx.Response:
        return self.request("GET", path)


@contextmanager
def airflow_api(env: dict):
    with httpx.Client(verify=False, timeout=30, headers={"Host": AIRFLOW_HOST}) as client:
        api = AirflowApi(client)
        login = {"username": env["AIRFLOW_ADMIN_USER"], "password": env["AIRFLOW_ADMIN_PASSWORD"]}
        token = api.request("POST", "/auth/token", json=login)
        assert token.status_code in (200, 201), token.text
        client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
        yield api


def poll(fetch, done, timeout: int, interval: float = 3):
    deadline = time.monotonic() + timeout
    while True:
        value = fetch()
        if done(value):
            return value
        assert time.monotonic() < deadline, f"timed out; last: {value}"
        time.sleep(interval)


def unpause_for_the_first_run(api: AirflowApi) -> str:
    """Unpausing a fresh `@daily` DAG without catchup queues a run of the latest whole interval,
    so a fresh deploy syncs at once; that run is the one returned."""
    # The dag-processor parses the mounted dags folder every few seconds.
    dag = poll(lambda: api.get(f"/api/v2/dags/{DAG_ID}"), lambda r: r.status_code == 200, 180)
    assert dag.json()["dag_id"] == DAG_ID
    unpaused = api.request("PATCH", f"/api/v2/dags/{DAG_ID}", json={"is_paused": False})
    assert unpaused.status_code == 200, unpaused.text
    runs = poll(lambda: api.get(f"/api/v2/dags/{DAG_ID}/dagRuns").json()["dag_runs"], bool, 120)
    (run,) = runs
    assert run["run_type"] == "scheduled", run
    return run["dag_run_id"]


def wait_for_dag_run(api: AirflowApi, run_id: str) -> str:
    finished = poll(
        lambda: api.get(f"/api/v2/dags/{DAG_ID}/dagRuns/{run_id}").json(),
        lambda doc: doc.get("state") in ("success", "failed"),
        300,
    )
    return finished["state"]


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
def test_connector_lifecycle_on_laptop_postgres(tmp_path):
    build = vecten("dev", "build-images", "--repo", str(REPO), "--json", cwd=tmp_path)
    assert build.returncode == 0, build.stdout + build.stderr
    init = vecten(
        "init", "p", "--name", "acme", "--users", "3", "--source", "demo", "--json", cwd=tmp_path
    )
    assert init.returncode == 0, init.stdout + init.stderr
    proj = tmp_path / "p"
    project = compose_project_name(proj, load_config(proj))
    drop_volumes(project)
    try:
        add = vecten("connector", "add", "demo", "--from", "rest", "--json", cwd=proj)
        assert add.returncode == 0, add.stdout + add.stderr
        # The skill replaces the scaffold body; the fixture connector stands in for its work.
        (proj / "connectors" / "demo" / "connector.py").write_text(FIXTURE.read_text())
        set_secret = subprocess.run(
            [sys.executable, "-m", "vecten.cli.app", "secret", "set", "DEMO_TOKEN", "--json"],
            cwd=proj,
            capture_output=True,
            text=True,
            input="t\n",
        )
        assert set_secret.returncode == 0, set_secret.stdout + set_secret.stderr
        # The skill declares the secret the connector reads; `up` then hands it to Airflow.
        config = load_config(proj)
        config.connector("demo").secrets = ["DEMO_TOKEN"]
        save_config(proj, config)
        dag_file = proj / "orchestration" / "dags" / f"{DAG_ID}.py"
        assert str(dag_file.relative_to(proj)) in json.loads(add.stdout)["dags"]
        rendered = dag_file.read_text()
        quick = rendered.replace(
            'schedule="@daily")', 'schedule="@daily", retry_delay=timedelta(seconds=1))'
        )
        assert quick != rendered
        dag_file.write_text("from datetime import timedelta\n" + quick)
        # The stack starts with the connector in place, so Airflow parses its DAG while the
        # steps below run on the laptop.
        up = vecten("up", "--json", cwd=proj)
        assert up.returncode == 0, up.stdout + up.stderr

        before = vecten("raw", "schema", "demo", "--json", cwd=proj)
        assert before.returncode == 0, before.stdout + before.stderr
        assert json.loads(before.stdout)["tables"] == []

        run = vecten("connector", "run", "demo", "--record", "--json", cwd=proj)
        assert run.returncode == 5, run.stdout + run.stderr
        doc = json.loads(run.stdout)
        assert doc["objects"]["good_a"]["rows"] == 2 and doc["objects"]["good_b"]["rows"] == 1
        assert doc["objects"]["bad"]["ok"] is False

        env = dotenv_values(proj / ".env", interpolate=False)
        with psycopg.connect(
            host="127.0.0.1",
            port=local_pg_published_port(),
            user="loader",
            password=env["LOADER_PASSWORD"],
            dbname="warehouse",
        ) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "select table_name from information_schema.tables "
                    "where table_schema = 'raw_demo'"
                )
            }
            assert {"good_a", "good_b"} <= tables and "bad" not in tables
            assert conn.execute("select count(*) from raw_demo.good_a").fetchone()[0] == 2
            assert conn.execute("select count(*) from raw_demo.good_b").fetchone()[0] == 1

        after = vecten("raw", "schema", "demo", "--json", cwd=proj)
        assert after.returncode == 0, after.stdout + after.stderr
        schema = json.loads(after.stdout)
        names = {t["name"] for t in schema["tables"]}
        assert {"good_a", "good_b"} <= names
        good_a = next(t for t in schema["tables"] if t["name"] == "good_a")
        assert {c["name"] for c in good_a["columns"]} >= {"id", "name"}

        record = json.loads((proj / ".vecten" / "records" / "connector-run-demo.json").read_text())
        assert record["objects"]["good_a"]["ok"] is True and record["recorded"] is True
        assert (proj / "connectors" / "demo" / "fixtures" / "good_a.jsonl").is_file()
        decl = tomllib.loads((proj / "vecten.toml").read_text())["connectors"][0]
        assert decl["source"] == "custom"

        replay = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "connectors/demo/test_demo.py",
                "-o",
                "filterwarnings=ignore",
            ],
            cwd=proj,
            capture_output=True,
            text=True,
        )
        assert replay.returncode == 0 and "1 passed" in replay.stdout, replay.stdout + replay.stderr

        env = dotenv_values(proj / ".env", interpolate=False)
        with airflow_api(env) as api:
            run_id = unpause_for_the_first_run(api)
            state = wait_for_dag_run(api, run_id)
            assert state == "failed"
            instances = api.get(f"/api/v2/dags/{DAG_ID}/dagRuns/{run_id}/taskInstances")
            assert instances.status_code == 200, instances.text
            by_task: dict[str, list[str]] = {}
            for ti in instances.json()["task_instances"]:
                by_task.setdefault(ti["task_id"], []).append(ti["state"])
            assert Counter(by_task["sync"]) == {"failed": 1, "success": 2}, by_task
            assert by_task["publish"] == ["success"] and by_task["check"] == ["failed"], by_task

        # `vecten issues` reads the same failure through the local Airflow API: the object's
        # task, keyed by the rendered map index, with the log the API serves.
        listed = vecten("issues", "--json", cwd=proj)
        doc = json.loads(listed.stdout)
        assert doc["sources"]["airflow"] == "ok" and doc["sources"]["dbt"] == "ok", doc
        (bad,) = [i for i in doc["issues"] if i["source"] == "airflow"]
        assert bad["id"].startswith(f"iss_airflow_{DAG_ID}_sync.bad_"), bad
        # The fixture's `bad` object fails with "upstream returned 500": an outage to wait out.
        assert bad["kind"] == "ask" and "demo source is failing upstream" in bad["resolution"]
        shown = vecten("issues", "show", bad["id"], "--json", cwd=proj)
        assert shown.returncode == 0, shown.stdout + shown.stderr
        evidence = json.loads(shown.stdout)["issue"]["evidence"]
        assert evidence["error_class"] != "TaskFailed" and evidence["excerpt"], evidence
        assert "ObjectSyncError: demo.bad:" in evidence["excerpt"], evidence

        with psycopg.connect(
            host="127.0.0.1",
            port=local_pg_published_port(),
            user="loader",
            password=env["LOADER_PASSWORD"],
            dbname="warehouse",
        ) as conn:
            # good_a's incremental cursor comes back from the warehouse, so it loads nothing new.
            assert conn.execute("select count(*) from raw_demo.good_a").fetchone()[0] == 2
            # One row from the recorded run, then one from the DAG run.
            good_b = conn.execute("select count(*) from raw_demo.good_b").fetchone()[0]
            assert good_b == 2
    finally:
        cleanup = vecten("down", "--volumes", "--json", cwd=proj)
        drop_volumes(project)
        remove_stack_images(project)
        assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr
