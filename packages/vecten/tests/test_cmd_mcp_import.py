import json
import shutil
from pathlib import Path

from .fixture_project import render_fixture

ARTIFACTS = Path(__file__).parents[2] / "vecten-shared" / "tests" / "fixtures" / "dbt"


def dbt_project(base, *, catalog=True, manifest=True):
    target = base / "analytics" / "target"
    target.mkdir(parents=True)
    if manifest:
        shutil.copy(ARTIFACTS / "manifest.json", target)
    if catalog:
        shutil.copy(ARTIFACTS / "catalog.json", target)
    return target.parent


def test_import_matches_the_build_output_and_leaves_metrics(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path / "p", "postgres")
    expected = (root / "osi" / "datasets.json").read_bytes()
    metrics = (root / "osi" / "metrics.json").read_bytes()
    (root / "osi" / "datasets.json").unlink()
    monkeypatch.chdir(root)
    r = invoke("mcp", "import", "--dbt", str(dbt_project(tmp_path)), "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    data = json.loads(r.stdout)
    assert data["changed"] is True and data["datasets"] >= 1
    assert (root / "osi" / "datasets.json").read_bytes() == expected
    assert (root / "osi" / "metrics.json").read_bytes() == metrics
    again = invoke("mcp", "import", "--dbt", str(dbt_project(tmp_path / "again")))
    assert again.exit_code == 0, again.stdout + again.stderr
    assert "unchanged" in again.stdout
    assert (root / "osi" / "datasets.json").read_bytes() == expected


def test_missing_catalog_says_run_docs_generate(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path / "p", "postgres")
    monkeypatch.chdir(root)
    r = invoke("mcp", "import", "--dbt", str(dbt_project(tmp_path, catalog=False)), "--json")
    assert r.exit_code == 3, r.stdout
    assert "dbt docs generate" in json.loads(r.stdout)["error"]


def test_not_a_dbt_project_says_run_docs_generate(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path / "p", "postgres")
    monkeypatch.chdir(root)
    (tmp_path / "empty").mkdir()
    r = invoke("mcp", "import", "--dbt", str(tmp_path / "empty"), "--json")
    assert r.exit_code == 3, r.stdout
    assert "dbt docs generate" in json.loads(r.stdout)["error"]


def test_a_schema_with_no_models_is_refused_and_keeps_the_file(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path / "p", "postgres")
    before = (root / "osi" / "datasets.json").read_bytes()
    monkeypatch.chdir(root)
    r = invoke("mcp", "import", "--dbt", str(dbt_project(tmp_path)), "--schema", "mart", "--json")
    assert r.exit_code == 6, r.stdout
    error = json.loads(r.stdout)["error"]
    assert "mart" in error and "marts" in error
    assert (root / "osi" / "datasets.json").read_bytes() == before


def test_broken_artifact_json_names_the_file(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path / "p", "postgres")
    project = dbt_project(tmp_path)
    (project / "target" / "manifest.json").write_text("{")
    monkeypatch.chdir(root)
    r = invoke("mcp", "import", "--dbt", str(project), "--json")
    assert r.exit_code == 3, r.stdout
    assert "manifest.json" in json.loads(r.stdout)["error"]


def test_generator_issues_exit_6(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path / "p", "postgres")
    project = dbt_project(tmp_path)
    catalog = json.loads((project / "target" / "catalog.json").read_text())
    catalog["nodes"] = {}
    (project / "target" / "catalog.json").write_text(json.dumps(catalog))
    monkeypatch.chdir(root)
    r = invoke("mcp", "import", "--dbt", str(project), "--json")
    assert r.exit_code == 6, r.stdout
    assert "not_in_catalog" in json.loads(r.stdout)["error"]
