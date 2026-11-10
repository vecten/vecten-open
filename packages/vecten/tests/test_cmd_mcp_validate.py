import hashlib
import json
import shutil

from vecten.next.semantic import osi_digest
from vecten_shared.records import read_record

from .fixture_project import render_fixture

LOCAL = ("--name", "acme", "--users", "3", "--source", "hubspot")


def test_fixture_model_is_valid_with_warnings(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert r.stdout.count("\n") == 1
    doc = json.loads(r.stdout)
    assert doc["ok"] is True and doc["errors"] == []
    assert [w["code"] for w in doc["warnings"]] == ["semi_additive_candidate"] * 2
    assert any("total_pipeline" in w["message"] for w in doc["warnings"])
    assert any("source existence skipped" in n for n in doc["notes"])
    assert doc["homes"]["total_pipeline"] == "sales_pipeline"


def test_human_output_lists_warnings_and_notes(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate")
    assert r.exit_code == 0, r.stderr
    assert "semi_additive_candidate" in r.stdout and "source existence skipped" in r.stdout


def test_yaml_document_is_a_config_error(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    (root / "osi" / "extra.yml").write_text("metrics: []")
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--json")
    assert r.exit_code == 6, r.stdout
    doc = json.loads(r.stdout)
    assert doc["ok"] is False and doc["code_name"] == "CONFIG"
    assert doc["errors"][0]["document"].endswith("extra.yml")
    assert "extra.yml: not_json_document" in doc["error"]


def test_fresh_project_shells_are_valid(invoke, workdir, monkeypatch):
    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    monkeypatch.chdir(workdir / "p")
    r = invoke("mcp", "validate", "--json")
    assert r.exit_code == 0, r.stdout
    assert "no datasets yet" in json.loads(r.stdout)["notes"]


def test_outside_a_project_is_refused(invoke):
    r = invoke("mcp", "validate", "--json")
    assert r.exit_code == 4
    assert json.loads(r.stdout)["code_name"] == "REFUSED"


def test_missing_osi_directory_is_a_config_error(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    shutil.rmtree(root / "osi")
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--json")
    assert r.exit_code == 6, r.stdout
    assert json.loads(r.stdout)["errors"][0]["code"] == "missing_osi_dir"


def test_validate_writes_a_record(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "snowflake")
    monkeypatch.chdir(root)
    assert invoke("mcp", "validate").exit_code == 0
    record = read_record(root, "mcp-validate")
    assert record["ok"] is True and record["errors"] == 0 and record["warnings"] == 2
    assert record["osi_sha256"] == osi_digest(root / "osi")
    (root / "osi" / "extra.yml").write_text("x: 1")
    assert invoke("mcp", "validate").exit_code == 6
    failed = read_record(root, "mcp-validate")
    assert failed["ok"] is False and failed["errors"] == 1
    assert failed["osi_sha256"] == osi_digest(root / "osi") != record["osi_sha256"]


def test_osi_digest_covers_names_and_bytes_and_skips_hidden_files(tmp_path):
    osi = tmp_path / "osi"
    (osi / "sub").mkdir(parents=True)
    (osi / "a.json").write_text("{}")
    (osi / "sub" / "b.json").write_text("[]")
    first = osi_digest(osi)
    (osi / ".DS_Store").write_text("noise")
    (osi / ".cache").mkdir()
    (osi / ".cache" / "x.json").write_text("noise")
    assert osi_digest(osi) == first
    (osi / "a.json").rename(osi / "c.json")
    assert osi_digest(osi) != first
    assert osi_digest(tmp_path / "missing") == hashlib.sha256(b"").hexdigest()


def _case_file(root, cases):
    import yaml

    (root / "evals").mkdir(exist_ok=True)
    (root / "evals" / "semantic.yaml").write_text(yaml.safe_dump({"cases": cases}))


def _good_case(root):
    from vecten_shared.osi.evals import sql_sha256
    from vecten_shared.osi.semantic import SemanticModel

    request = {"metrics": ["total_pipeline"]}
    sql = SemanticModel.load(root / "osi").compile(request)["sql"]
    expect = {
        "outcome": "compiled",
        "metrics": ["total_pipeline"],
        "dimensions": [],
        "grain": None,
        "sql_sha256": sql_sha256(sql),
    }
    return {"name": "pipeline", "request": request, "expect": expect}


def test_valid_model_reports_counts(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--json")
    counts = json.loads(r.stdout)["counts"]
    assert counts["datasets"] >= 1 and counts["metrics"] == 3
    human = invoke("mcp", "validate")
    assert f"{counts['datasets']} datasets, 3 metrics" in human.stdout


def test_evals_pass(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    _case_file(root, [_good_case(root)])
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--evals", "--json")
    assert r.exit_code == 0, r.stdout
    evals = json.loads(r.stdout)["evals"]
    assert (evals["passed"], evals["failed"]) == (1, 0)
    assert evals["cases"][0]["name"] == "pipeline" and evals["cases"][0]["ok"] is True


def test_drifted_case_fails_with_the_new_sql(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    case = _good_case(root)
    case["expect"]["sql_sha256"] = "0" * 64
    case["gap"] = "gap_9"
    _case_file(root, [case])
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--evals")
    out = r.stdout + r.stderr
    assert r.exit_code == 6, out
    assert "case pipeline (gap gap_9)" in out and "SELECT" in out
    record = read_record(root, "mcp-validate")
    assert record["evals_failed"] == 1 and record["ok"] is False
    from vecten.next.semantic import _model_items

    assert any(item.title == "Validate the semantic model" for item in _model_items(root))


def test_a_malformed_evals_file_records_a_failed_validation(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    _case_file(root, [{"name": "broken", "request": {}}])
    monkeypatch.chdir(root)
    assert invoke("mcp", "validate", "--evals").exit_code == 6
    assert read_record(root, "mcp-validate")["ok"] is False


def test_malformed_evals_exit_6_naming_the_case(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    _case_file(root, [{"name": "broken", "request": {}}])
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--evals", "--json")
    assert r.exit_code == 6, r.stdout
    assert "broken" in json.loads(r.stdout)["error"]


def test_scaffolded_empty_evals_pass(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--evals", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["evals"]["passed"] == 0


def test_evals_flag_without_a_file_notes_it(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    (root / "evals" / "semantic.yaml").unlink()
    monkeypatch.chdir(root)
    r = invoke("mcp", "validate", "--evals", "--json")
    assert r.exit_code == 0, r.stdout
    assert "no evals/semantic.yaml" in json.loads(r.stdout)["notes"]


def test_plain_validate_ignores_evals(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    (root / "evals" / "semantic.yaml").write_text("not: [valid")
    monkeypatch.chdir(root)
    assert invoke("mcp", "validate").exit_code == 0
