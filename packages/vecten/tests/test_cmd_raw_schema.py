import json

import dlt
import pytest

from vecten.connectors import raw as raw_mod
from vecten_platform.sync.runner import pipeline_name

LOCAL = ("--name", "acme", "--users", "3", "--source", "hubspot")


@pytest.fixture
def project(invoke, workdir, monkeypatch, tmp_path):
    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    root = workdir / "p"
    monkeypatch.chdir(root)
    (root / ".env").write_text("LOADER_PASSWORD=unused\n")
    dest = dlt.destinations.duckdb(str(tmp_path / "wh.duckdb"))
    monkeypatch.setattr(raw_mod, "make_destination", lambda ctx: dest)
    return root, dest, tmp_path


def test_the_schema_pipeline_cannot_collide_with_an_object_pipeline():
    assert raw_mod.schema_pipeline_name("hubspot") == "vecten_schema__hubspot"
    assert raw_mod.schema_pipeline_name("hubspot") != pipeline_name("hubspot", "schema")


def test_warehouse_down_is_prerequisite(invoke, project, monkeypatch):
    from vecten_platform.sync.destination import build_destination
    from vecten_shared.provider import ConnectionSpec

    spec = ConnectionSpec(
        kind="postgres",
        database="warehouse",
        role="loader",
        auth="password",
        host="127.0.0.1",
        port=1,
    )
    monkeypatch.setattr(raw_mod, "make_destination", lambda ctx: build_destination(spec, "x"))
    r = invoke("raw", "schema", "hubspot", "--json")
    assert r.exit_code == 3, r.stdout + r.stderr
    assert "the warehouse is not reachable; run `vecten up`" in json.loads(r.stdout)["error"]


def test_empty_before_any_run(invoke, project):
    r = invoke("raw", "schema", "hubspot", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {
        "source": "hubspot",
        "schema": "raw_hubspot",
        "tables": [],
        "ok": True,
    }


def test_lists_tables_and_columns_after_a_load(invoke, project):
    root, dest, tmp = project
    pipe = dlt.pipeline(
        "hubspot__companies",
        destination=dest,
        dataset_name="raw_hubspot",
        pipelines_dir=str(tmp / "dlt"),
    )
    pipe.run([{"id": 1, "name": "a", "created_at": "2024-01-01T00:00:00Z"}], table_name="companies")
    r = invoke("raw", "schema", "hubspot", "--json")
    assert r.exit_code == 0
    doc = json.loads(r.stdout)
    tables = {t["name"]: {c["name"]: c["type"] for c in t["columns"]} for t in doc["tables"]}
    assert set(tables) == {"companies"}
    assert set(tables["companies"]) >= {"id", "name", "created_at"}
    assert all(not name.startswith("_dlt") for name in tables)


def test_human_output_is_a_table_listing(invoke, project):
    r = invoke("raw", "schema", "hubspot")
    assert r.exit_code == 0 and "raw_hubspot" in r.stdout and "no tables" in r.stdout


def test_requires_json_flag_to_be_accepted_positionally(invoke, project):
    r = invoke("raw", "schema", "--json", "hubspot")
    assert r.exit_code == 0


def test_bad_name_is_usage(invoke, project):
    r = invoke("raw", "schema", "Hub-Spot", "--json")
    assert r.exit_code == 2


def test_missing_name_is_usage(invoke, project):
    r = invoke("raw", "schema", "--json")
    assert r.exit_code == 2
    assert "source name" in json.loads(r.stdout)["error"]
