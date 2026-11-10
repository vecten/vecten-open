import hashlib
import json
import shutil

import pytest

from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config, save_config
from vecten_shared.records import read_record

from .docker_util import docker_available
from .fixture_project import render_fixture


def _one_document(result) -> dict:
    assert result.stdout.count("\n") == 1, result.output
    return json.loads(result.stdout)


def _hand_edit(path):
    # dbt parses the Ossie documents and fails on a metric whose dataset is gone, so the edit
    # keeps every dataset.
    text = path.read_text()
    edited = text.replace('"is_time": true', '"is_time": false', 1)
    assert edited != text
    path.write_text(edited)
    return text, edited


def test_build_local_regenerates_datasets_and_records(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    datasets = root / "osi" / "datasets.json"
    _, before = _hand_edit(datasets)
    metrics_before = (root / "osi" / "metrics.json").read_bytes()
    result = invoke("dbt-build-local", "--json")
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    data = _one_document(result)
    assert data["ok"] and data["datasets_changed"]
    assert data["replayed"] == {"demo": ["company", "deal"]}
    assert data["kind"] == "snowflake" and data["stale"] is False
    after = datasets.read_text()
    assert after != before and '"sales_pipeline"' in after
    assert (root / "osi" / "metrics.json").read_bytes() == metrics_before
    record = read_record(root, "dbt-build-local")
    assert record["ok"] and record["datasets_sha256"] == hashlib.sha256(after.encode()).hexdigest()
    assert record["warehouse_kind"] == "snowflake" and record["failed"] == []
    assert not (root / ".vecten" / "dbt-local").exists()


def test_changed_description_changes_the_generated_file(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    first = invoke("dbt-build-local")
    assert first.exit_code == 0, first.output
    assert "osi/datasets.json unchanged" in first.stdout
    assert "replaying connector fixtures" in first.stderr
    yml = root / "dbt" / "models" / "marts" / "schema.yml"
    original = yml.read_text()
    changed = original.replace(
        "ISO 3166-1 alpha-2 country code of the company.", "ISO country of the buyer."
    )
    assert changed != original
    yml.write_text(changed)
    second = invoke("dbt-build-local")
    assert second.exit_code == 0, second.output
    assert "osi/datasets.json regenerated" in second.stdout
    assert "ISO country of the buyer." in (root / "osi" / "datasets.json").read_text()


def test_check_fails_on_a_hand_edited_datasets_file(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    assert invoke("dbt-build-local").exit_code == 0
    path = root / "osi" / "datasets.json"
    committed, edited = _hand_edit(path)
    result = invoke("dbt-build-local", "--check", "--json")
    assert result.exit_code == 6
    doc = _one_document(result)
    assert "osi/datasets.json" in doc["error"] and doc["stale"] is True
    assert path.read_text() == edited
    record = read_record(root, "dbt-build-local")
    assert record["ok"] is True and record["stale"] is True
    assert invoke("dbt-build-local", "--check").exit_code == 6
    path.write_text(committed)
    assert invoke("dbt-build-local", "--check").exit_code == 0


def test_convention_failure_forwards_the_node_and_keeps_datasets(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    before = (root / "osi" / "datasets.json").read_text()
    model = root / "dbt" / "models" / "staging" / "staging_demo_deal.sql"
    model.write_text(model.read_text().replace("'demo' as _source", "'demo' as _origin"))
    result = invoke("dbt-build-local", "--json")
    assert result.exit_code == 5
    doc = _one_document(result)
    assert doc["error"] == (
        "dbt failed: convention_staging_load_metadata: "
        "convention violations: model staging_demo_deal: lacks column _source"
    )
    assert doc["failed"] and doc["code_name"] == "TOOL_FAILED"
    assert (root / "osi" / "datasets.json").read_text() == before
    record = read_record(root, "dbt-build-local")
    assert record["ok"] is False and record["failed"]
    assert not (root / ".vecten" / "dbt-local").exists()


def test_build_local_refuses_connector_without_fixtures(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    shutil.rmtree(root / "connectors" / "demo" / "fixtures")
    result = invoke("dbt-build-local", "--json")
    assert result.exit_code == 3
    assert "vecten connector run demo --record" in _one_document(result)["error"]


def test_build_local_refuses_mcp_only_project(invoke, tmp_path, monkeypatch):
    answers = {"name": "acme", "users": 2, "warehouse_url": "postgres://h:5432/dw"}
    render_project(tmp_path / "p", ScaffoldParams.from_answers(answers, {}))
    monkeypatch.chdir(tmp_path / "p")
    assert invoke("dbt-build-local").exit_code == 4


def test_build_local_outside_a_project_is_refused(invoke):
    result = invoke("dbt-build-local", "--json")
    assert result.exit_code == 4
    assert _one_document(result)["code_name"] == "REFUSED"


def test_postgres_project_without_docker_names_the_prerequisite(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd: None if cmd == "docker" else real_which(cmd))
    result = invoke("dbt-build-local", "--json")
    assert result.exit_code == 3
    assert "Docker is needed" in _one_document(result)["error"]
    assert not (root / ".vecten" / "dbt-local").exists()


def test_a_mart_without_a_key_fails_every_build_and_keeps_datasets(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    yml = root / "dbt" / "models" / "marts" / "schema.yml"
    described = "        description: Deal identifier in the demo source.\n"
    keyed = described + "        data_tests: [unique, not_null]\n"
    assert keyed in yml.read_text()
    yml.write_text(yml.read_text().replace(keyed, described))
    datasets = root / "osi" / "datasets.json"
    before = datasets.read_bytes()
    for _ in range(2):
        result = invoke("dbt-build-local", "--json")
        assert result.exit_code == 6, result.output
        error = _one_document(result)["error"]
        assert "mart sales_pipeline declares no primary key" in error
        assert "meta.osi.primary_key" in error
        assert datasets.read_bytes() == before
    assert read_record(root, "dbt-build-local")["ok"] is False


def test_renaming_a_mart_after_editing_its_metric_builds(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    marts = root / "dbt" / "models" / "marts"
    (marts / "company_overview.sql").rename(marts / "company_summary.sql")
    yml = marts / "schema.yml"
    yml.write_text(yml.read_text().replace("- name: company_overview", "- name: company_summary"))
    metrics = root / "osi" / "metrics.json"
    metrics.write_text(metrics.read_text().replace("company_overview", "company_summary"))
    result = invoke("dbt-build-local", "--json")
    assert result.exit_code == 0, result.output
    text = (root / "osi" / "datasets.json").read_text()
    assert '"company_summary"' in text and "company_overview" not in text


def test_the_build_database_is_the_configured_one(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    config = load_config(root)
    warehouse = config.warehouse.model_copy(update={"database": "analytics"})
    save_config(root, config.model_copy(update={"warehouse": warehouse}))
    result = invoke("dbt-build-local", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads((root / "osi" / "datasets.json").read_text())
    sources = [d["source"] for d in doc["semantic_model"][0]["datasets"]]
    assert sources and all(s.startswith("analytics.marts.") for s in sources), sources
    # The second build reads the regenerated sources back through dbt's parse.
    again = invoke("dbt-build-local", "--check", "--json")
    assert again.exit_code == 0, again.output


def test_a_parse_error_names_dbt_message_and_leaves_the_log(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    deal = root / "dbt" / "models" / "core" / "deal.sql"
    deal.write_text(deal.read_text() + "\n{{ ref('company' }}\n")
    result = invoke("dbt-build-local", "--json")
    assert result.exit_code == 5, result.output
    error = _one_document(result)["error"]
    assert error.startswith("dbt failed: ") and "deal" in error
    assert "no failing node was recorded" not in error
    assert (root / "dbt" / "logs" / "dbt.log").is_file()


def test_a_fresh_project_without_connectors_passes_its_own_check(invoke, tmp_path, monkeypatch):
    root = tmp_path / "proj"
    answers = {"name": "acme-data", "users": 3, "sources": ["hubspot"], "warehouse": "snowflake"}
    render_project(root, ScaffoldParams.from_answers(answers, {"components": ["platform"]}))
    monkeypatch.chdir(root)
    result = invoke("dbt-build-local", "--check", "--json")
    assert result.exit_code == 0, result.output
    assert _one_document(result)["stale"] is False


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="docker not reachable")
def test_postgres_build_leaves_the_committed_datasets_fresh(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    result = invoke("dbt-build-local", "--check", "--json")
    assert result.exit_code == 0, result.output
    data = _one_document(result)
    assert data["kind"] == "postgres" and data["stale"] is False
    assert data["replayed"] == {"demo": ["company", "deal"]}
    assert read_record(root, "dbt-build-local")["ok"] is True


def _value_case(root, kind, equals, tolerance=0.0):
    import yaml

    from vecten_shared.osi.evals import sql_sha256
    from vecten_shared.osi.semantic import SemanticModel

    request = {"metrics": ["deal_count"]}
    sql = SemanticModel.load(root / "osi", kind=kind).compile(request)["sql"]
    case = {
        "name": "deals",
        "gap": "gap_3",
        "request": request,
        "expect": {
            "outcome": "compiled",
            "sql_sha256": sql_sha256(sql),
            "value": {"metric": "deal_count", "equals": equals, "tolerance": tolerance},
        },
    }
    (root / "evals" / "semantic.yaml").write_text(yaml.safe_dump({"cases": [case]}))


@pytest.mark.parametrize(
    "kind",
    [
        "snowflake",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.docker,
                pytest.mark.skipif(not docker_available(), reason="docker not reachable"),
            ],
        ),
    ],
)
def test_eval_values_are_checked_against_the_built_data(invoke, tmp_path, monkeypatch, kind):
    import re

    root = render_fixture(tmp_path, kind)
    monkeypatch.chdir(root)
    _value_case(root, kind, -1)
    failed = invoke("dbt-build-local", "--json")
    assert failed.exit_code == 6, failed.output
    error = json.loads(failed.stdout)["error"]
    assert "case deals (gap gap_3)" in error
    actual = float(re.search(r"deal_count is ([0-9.]+), expected", error).group(1))
    assert actual > 0
    _value_case(root, kind, actual, 0.001)
    passed = invoke("dbt-build-local", "--json")
    assert passed.exit_code == 0, passed.output
    assert _one_document(passed)["evals"] == {"checked": 1, "failed": []}
    assert read_record(root, "dbt-build-local")["evals"]["checked"] == 1
