import json
import shutil
import tomllib
from pathlib import Path

import dlt
import pytest
import yaml

from vecten.connectors import run as run_mod
from vecten.connectors.render import scaffold_hash
from vecten_shared.config import ConnectorDecl, load_config, save_config
from vecten_shared.records import read_record

PLATFORM_TESTS = (
    Path(__file__).resolve().parents[2] / "vecten-platform" / "tests" / "fixture_connector" / "demo"
)
LOCAL = ("--name", "acme", "--users", "3", "--source", "demo")


@pytest.fixture
def project(invoke, workdir, monkeypatch, tmp_path):
    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    root = workdir / "p"
    monkeypatch.chdir(root)
    # The fixture connector stands in for a source the user wrote; register it by hand.
    shutil.copytree(PLATFORM_TESTS, root / "connectors" / "demo")
    (root / "connectors" / "demo" / "fixtures").mkdir(exist_ok=True)
    config = load_config(root)
    config.wanted = []
    config.connectors.append(ConnectorDecl(name="demo", source="custom", secrets=["DEMO_TOKEN"]))
    save_config(root, config)
    (root / ".env").write_text("DEMO_TOKEN=t\nLOADER_PASSWORD=unused\n")
    dest = dlt.destinations.duckdb(str(tmp_path / "wh.duckdb"))
    monkeypatch.setattr(run_mod, "make_destination", lambda ctx: dest)
    return root


def _connector_decl(project: Path) -> dict:
    return tomllib.loads((project / "vecten.toml").read_text())["connectors"][0]


def test_run_all_objects_reports_rows_and_fails_on_the_bad_one(invoke, project):
    r = invoke("connector", "run", "demo", "--json")
    assert r.exit_code == 5, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["ok"] is False
    assert doc["objects"]["good_a"] == {"ok": True, "rows": 2, "error": None}
    assert doc["objects"]["good_b"]["ok"] is True
    assert doc["objects"]["bad"]["ok"] is False
    assert "upstream returned 500" in doc["objects"]["bad"]["error"]
    record = read_record(project, "connector-run-demo")
    assert record["ok"] is False and record["objects"]["good_a"]["ok"] is True
    sources = yaml.safe_load((project / "dbt" / "models" / "staging" / "sources.yml").read_text())
    demo = next(s for s in sources["sources"] if s["name"] == "demo")
    assert [t["name"] for t in demo["tables"]] == ["good_a", "good_b"]


def test_run_single_object(invoke, project):
    r = invoke("connector", "run", "demo", "--object", "good_a", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert list(doc["objects"]) == ["good_a"] and doc["objects"]["good_a"]["rows"] == 2
    record = read_record(project, "connector-run-demo")
    assert record["ok"] is True and list(record["objects"]) == ["good_a"]
    human = invoke("connector", "run", "demo", "--object", "good_a")
    assert "good_a: 0 rows" in human.stdout


def test_run_unknown_object_is_usage(invoke, project):
    r = invoke("connector", "run", "demo", "--object", "nope", "--json")
    assert r.exit_code == 2
    assert "good_a, bad, good_b" in json.loads(r.stdout)["error"]


def test_run_record_writes_fixtures(invoke, project):
    r = invoke("connector", "run", "demo", "--object", "good_b", "--record", "--json")
    assert r.exit_code == 0
    assert json.loads(r.stdout)["recorded"] is True
    fixture = project / "connectors" / "demo" / "fixtures" / "good_b.jsonl"
    assert fixture.read_text().splitlines() == ['{"id": 10, "amount": 1.5}']
    assert read_record(project, "connector-run-demo")["recorded"] is True


def test_run_source_not_added_suggests_add(invoke, project):
    r = invoke("connector", "run", "stripe", "--json")
    assert r.exit_code == 4
    assert "vecten connector add stripe" in json.loads(r.stdout)["error"]


def test_run_without_a_name_is_usage(invoke, project):
    r = invoke("connector", "run", "--json")
    assert r.exit_code == 2
    assert "source name" in json.loads(r.stdout)["error"]


def test_run_bad_name_is_usage(invoke, project):
    r = invoke("connector", "run", "../demo", "--json")
    assert r.exit_code == 2


def test_run_missing_secret_is_prerequisite(invoke, project):
    (project / ".env").write_text("LOADER_PASSWORD=unused\n")
    r = invoke("connector", "run", "demo", "--json")
    assert r.exit_code == 3
    assert "vecten secret set DEMO_TOKEN" in json.loads(r.stdout)["error"]
    assert read_record(project, "connector-run-demo") is None


def test_run_source_construction_failure(invoke, project):
    (project / "connectors" / "demo" / "connector.py").write_text(
        "def source():\n    raise ValueError('bad config')\n"
    )
    r = invoke("connector", "run", "demo", "--json")
    assert r.exit_code == 5
    doc = json.loads(r.stdout)
    assert "ValueError" in doc["error"] and "bad config" in doc["error"]
    record = read_record(project, "connector-run-demo")
    assert record["ok"] is False and record["objects"] == {}


CRM_CONNECTOR = """
import dlt


@dlt.resource(name="Contact-Lists")
def contact_lists():
    yield from [{"id": 1, "members": [{"m": "a"}, {"m": "b"}]}, {"id": 2, "members": []}]


@dlt.source(name="demo")
def demo_source():
    return [contact_lists]


def source():
    return demo_source()
"""


def test_run_adds_the_tables_that_landed_to_sources_yml(invoke, project):
    (project / "connectors" / "demo" / "connector.py").write_text(CRM_CONNECTOR)
    r = invoke("connector", "run", "demo", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["objects"]["Contact-Lists"]["rows"] == 2
    assert doc["tables"] == ["contact_lists"]
    sources = yaml.safe_load((project / "dbt" / "models" / "staging" / "sources.yml").read_text())
    demo = next(s for s in sources["sources"] if s["name"] == "demo")
    assert [t["name"] for t in demo["tables"]] == ["contact_lists"]


def test_run_of_one_object_keeps_the_other_objects_in_the_record(invoke, project):
    assert invoke("connector", "run", "demo", "--json").exit_code == 5
    r = invoke("connector", "run", "demo", "--object", "good_a", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert list(json.loads(r.stdout)["objects"]) == ["good_a"]
    record = read_record(project, "connector-run-demo")
    assert set(record["objects"]) == {"good_a", "bad", "good_b"}
    assert record["objects"]["bad"]["ok"] is False
    assert record["objects"]["good_a"] == {"ok": True, "rows": 0, "error": None}
    assert record["ok"] is False


def closed_port_destination(ctx):
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
    return build_destination(spec, "unused")


def test_run_with_the_warehouse_down_is_prerequisite(invoke, project, monkeypatch):
    monkeypatch.setattr(run_mod, "make_destination", closed_port_destination)
    r = invoke("connector", "run", "demo", "--record", "--json")
    assert r.exit_code == 3, r.stdout + r.stderr
    assert "the warehouse is not reachable; run `vecten up`" in json.loads(r.stdout)["error"]
    assert read_record(project, "connector-run-demo") is None
    assert list((project / "connectors" / "demo" / "fixtures").iterdir()) == []


def test_run_flips_a_generic_scaffold_to_custom_when_edited(invoke, project):
    text = (project / "vecten.toml").read_text()
    text = text.replace('source = "custom"', 'source = "rest"\nscaffold_hash = "0000"')
    (project / "vecten.toml").write_text(text)
    assert invoke("connector", "run", "demo", "--object", "good_a", "--json").exit_code == 0
    decl = _connector_decl(project)
    assert decl["source"] == "custom" and "scaffold_hash" not in decl


def test_run_keeps_a_generic_kind_while_the_scaffold_is_untouched(invoke, project):
    digest = scaffold_hash((project / "connectors" / "demo" / "connector.py").read_text())
    text = (project / "vecten.toml").read_text()
    text = text.replace('source = "custom"', f'source = "rest"\nscaffold_hash = "{digest}"')
    (project / "vecten.toml").write_text(text)
    assert invoke("connector", "run", "demo", "--object", "good_a", "--json").exit_code == 0
    assert _connector_decl(project)["source"] == "rest"
