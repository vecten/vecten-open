import json
import logging
import subprocess

import pytest

from vecten_platform.dbt.local_warehouse import disposable_postgres, snowflake_emulation
from vecten_platform.dbt.raw_replay import missing_fixtures, replay_fixtures
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure

from .docker_util import docker_available


def _fakesnow_connect(schema: str):
    import snowflake.connector

    return snowflake.connector.connect(
        account="fakesnow", user="u", password="p", database="WAREHOUSE", schema=schema
    )


def _pg_connect(target):
    import psycopg

    return psycopg.connect(
        host=target.spec.host,
        port=target.spec.port,
        user=target.spec.role,
        password=target.password,
        dbname="warehouse",
    )


def test_importing_the_local_build_modules_loads_no_heavy_dependency():
    import sys

    code = (
        "import sys, vecten_platform.dbt.local_warehouse, vecten_platform.dbt.raw_replay\n"
        "print(sorted({'dlt', 'dbt', 'fakesnow', 'psycopg2'} & set(sys.modules)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_snowflake_emulation_answers_queries(tmp_path):
    with snowflake_emulation(tmp_path) as target:
        conn = _fakesnow_connect("PUBLIC")
        assert conn.cursor().execute("select 1 + 1").fetchone()[0] == 2
        assert target.kind == "snowflake" and target.env["DBT_THREADS"] == "1" and target.in_process
        assert target.spec.database == "WAREHOUSE"
    assert (tmp_path / "fakesnow" / "WAREHOUSE.db").is_file()


def test_snowflake_emulation_keeps_sqlglot_warnings_quiet(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    before = logging.getLogger("sqlglot").level
    with snowflake_emulation(tmp_path):
        _fakesnow_connect("PUBLIC").cursor().execute("show user functions in WAREHOUSE.PUBLIC")
    assert not [r for r in caplog.records if r.name.startswith("sqlglot")]
    assert logging.getLogger("sqlglot").level == before


def test_postgres_target_needs_docker(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(Failure) as exc:
        with disposable_postgres("acme"):
            pass
    assert exc.value.code == ExitCode.PREREQUISITE and "Docker" in exc.value.message


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="docker not reachable")
def test_disposable_postgres_is_removed_afterwards():
    with disposable_postgres("acme") as target:
        with _pg_connect(target) as conn:
            assert conn.execute("select 1").fetchone() == (1,)
        assert target.kind == "postgres" and not target.in_process
        assert target.env["WAREHOUSE_PORT"] == str(target.spec.port)
        name = target.env["VECTEN_LOCAL_PG_CONTAINER"]
        volumes = container_volumes(name)
        assert volumes
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={name}", "-q"], capture_output=True, text=True
    ).stdout
    assert out.strip() == ""
    assert not [volume for volume in volumes if volume_exists(volume)]


def container_volumes(name):
    template = '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}'
    return subprocess.run(
        ["docker", "inspect", "-f", template, name], capture_output=True, text=True, check=True
    ).stdout.split()


def volume_exists(name):
    return (
        subprocess.run(["docker", "volume", "inspect", name], capture_output=True).returncode == 0
    )


def _project(tmp_path):
    fixtures = tmp_path / "connectors" / "demo" / "fixtures"
    fixtures.mkdir(parents=True)
    rows = [
        {"id": 1, "name": "Acme", "created_at": "2026-01-01T10:00:00+00:00"},
        {"id": 2, "name": "Beta", "created_at": "2026-01-02T10:00:00+00:00"},
    ]
    (fixtures / "company.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    config = VectenConfig.model_validate(
        {
            "users": 2,
            "project": {"name": "acme", "scaffold_version": "0.1.0"},
            "connectors": [
                {"name": "demo", "source": "custom"},
                {"name": "empty", "source": "custom"},
            ],
        }
    )
    return tmp_path / "connectors", config


def test_replay_into_the_emulation_lands_raw_tables(tmp_path):
    connectors_dir, config = _project(tmp_path)
    assert missing_fixtures(connectors_dir, config) == ["empty"]
    with snowflake_emulation(tmp_path) as target:
        loaded = replay_fixtures(target, connectors_dir, config)
        assert loaded == {"demo": ["company"], "empty": []}
        cur = _fakesnow_connect("RAW_DEMO").cursor()
        assert cur.execute("select count(*) from RAW_DEMO.COMPANY").fetchone()[0] == 2


def test_a_second_emulation_in_the_same_workdir_starts_empty(tmp_path):
    connectors_dir, config = _project(tmp_path)
    for _ in range(2):
        with snowflake_emulation(tmp_path / "work") as target:
            replay_fixtures(target, connectors_dir, config)
            cur = _fakesnow_connect("RAW_DEMO").cursor()
            assert cur.execute("select count(*) from RAW_DEMO.COMPANY").fetchone()[0] == 2


def test_a_failed_replay_is_a_tool_failure_naming_the_connector(tmp_path):
    connectors_dir, config = _project(tmp_path)
    (connectors_dir / "demo" / "fixtures" / "company.jsonl").write_text("not json\n")
    with snowflake_emulation(tmp_path / "work") as target:
        with pytest.raises(Failure) as failed:
            replay_fixtures(target, connectors_dir, config)
    assert failed.value.code == ExitCode.TOOL_FAILED
    assert failed.value.message.startswith("replaying connector demo failed: ")


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="docker not reachable")
def test_replay_into_postgres_lands_raw_tables(tmp_path):
    connectors_dir, config = _project(tmp_path)
    with disposable_postgres("acme") as target:
        for _ in range(2):
            assert replay_fixtures(target, connectors_dir, config)["demo"] == ["company"]
            with _pg_connect(target) as conn:
                assert conn.execute("select count(*) from raw_demo.company").fetchone() == (2,)
