import json
from importlib import resources

import pytest
import yaml

from vecten.knowhow import load_skill, skill_names
from vecten_shared.osi.evals import sql_sha256
from vecten_shared.osi.generate import generate_datasets
from vecten_shared.osi.semantic import SemanticModel

EVALS = resources.files("vecten.knowhow").joinpath("skills/semantic/evals")
SCENARIOS = ["wrong-definition", "mart-column", "empty-model"]


def grader_fixture(scenario):
    return json.loads(EVALS.joinpath(f"grader/{scenario}.json").read_text())


def test_semantic_skill_is_shipped_and_has_three_executable_examples():
    assert "semantic" in skill_names()
    assert load_skill("semantic")
    cases = json.loads(EVALS.joinpath("evals.json").read_text())["evals"]
    assert len(cases) >= 3


def test_the_agent_is_handed_no_fixture():
    """The gap reaches the agent through the MCP and the warehouse through the project; the
    expected figures and the adversarial question are the grader's alone."""
    cases = json.loads(EVALS.joinpath("evals.json").read_text())["evals"]
    assert [case["files"] for case in cases] == [[]] * len(cases)
    assert not EVALS.joinpath("files").is_dir()
    assert sorted(p.name for p in EVALS.joinpath("grader").iterdir()) == sorted(
        f"{name}.json" for name in SCENARIOS
    )


@pytest.mark.parametrize("scenario", ["wrong-definition", "mart-column"])
def test_the_gap_cases_name_the_refunds_scenario(scenario):
    fixture = grader_fixture(scenario)
    assert fixture["dataset"] == "sales_pipeline" and fixture["primary_key"] == "deal_id"
    assert fixture["gap"]["datasets"] == ["sales_pipeline"]
    assert "refunded_amount" in fixture["warehouse_fields"]
    assert ("refunded_amount" in fixture["initial_mart_fields"]) is (scenario == "wrong-definition")
    assert "refund_amount" not in json.dumps(fixture)


def test_the_empty_model_case_has_no_gap_and_expects_no_pull_request():
    fixture = grader_fixture("empty-model")
    assert "gap" not in fixture and fixture["dataset"] == "orders"
    case = json.loads(EVALS.joinpath("evals.json").read_text())["evals"][2]
    assert [e for e in case["expectations"] if "gap" in e.lower() or "PR" in e] == []


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_semantic_worked_examples_validate_and_detect_regression(
    invoke, tmp_path, monkeypatch, scenario
):
    """Replay the authored fixture outputs; this does not simulate an independent agent."""
    fixture = grader_fixture(scenario)
    args = ["init", "project", "--name", "demo", "--users", "3"]
    args += (
        ["--warehouse-url", "postgresql://localhost/warehouse"]
        if scenario == "empty-model"
        else ["--source", "start empty"]
    )
    result = invoke(*args, "--json")
    assert result.exit_code == 0, result.stdout
    root = tmp_path / "project"
    fields, dataset = fixture["warehouse_fields"], fixture["dataset"]
    node = {
        "unique_id": f"model.demo.{dataset}",
        "resource_type": "model",
        "alias": dataset,
        "schema": "marts",
        "description": "Rows with recorded refunds.",
        "meta": {"osi": {"primary_key": [fixture["primary_key"]]}},
        "columns": {name: {"name": name, "description": name.replace("_", " ")} for name in fields},
        "depends_on": {"nodes": []},
    }
    manifest = {"nodes": {node["unique_id"]: node}}
    catalog = {
        "nodes": {
            node["unique_id"]: {
                "metadata": {"schema": "marts", "name": dataset},
                "columns": {
                    name: {
                        "name": name,
                        "type": "TEXT" if name == "country" else "NUMERIC",
                        "index": i,
                    }
                    for i, name in enumerate(fields)
                },
            }
        }
    }
    datasets = generate_datasets(manifest, catalog, database="warehouse", model_name="demo")
    (root / "osi/datasets.json").write_text(json.dumps(datasets))
    if scenario == "mart-column":
        mart = root / f"dbt/models/marts/{dataset}.sql"
        mart.write_text(f"select {', '.join(fields)} from {{{{ ref('deal') }}}}\n")
    question = fixture["question"][:4096]
    metric = {
        "name": "total_revenue",
        "description": "Revenue less recorded refunds.",
        "expression": {
            "dialects": [{"dialect": "ANSI_SQL", "expression": fixture["fixed_expression"]}]
        },
        "ai_context": {"examples": [question]},
    }
    doc = {
        "version": "0.1.1",
        "semantic_model": [{"name": "metrics", "datasets": [], "metrics": [metric]}],
    }
    path = root / "osi/metrics.json"
    path.write_text(json.dumps(doc))
    model = SemanticModel.load(root / "osi", kind="postgres")
    request = {"metrics": ["total_revenue"]}
    compiled = model.compile(request)
    case = {
        "name": "revenue-refunds",
        "question": question,
        "request": request,
        "expect": {"outcome": "compiled", "sql_sha256": sql_sha256(compiled["sql"])},
    }
    if "gap" in fixture:
        case["gap"] = fixture["gap"]["id"]
    (root / "evals/semantic.yaml").write_text(yaml.safe_dump({"cases": [case]}))
    monkeypatch.chdir(root)
    result = invoke("mcp", "validate", "--evals", "--json")
    assert result.exit_code == 0, result.stdout
    if scenario == "empty-model":
        from pathlib import Path

        monkeypatch.syspath_prepend(str(Path(__file__).parent / "fixtures"))
        from eval_harness.semantic_grading import evidence

        case["expect"]["value"] = {"metric": "total_revenue", "equals": fixture["expected_revenue"]}
        (root / "evals/semantic.yaml").write_text(yaml.safe_dump({"cases": [case]}))
        (entry,) = evidence(root, "semantic-3")["matching_cases"]
        assert entry["matches_reference_expression"] and entry["pins_reference_value"]
        (root / "evals/semantic.yaml").write_text("cases: []")
        assert evidence(root, "semantic-3")["matching_cases"] == []
        (root / "evals/semantic.yaml").write_text(yaml.safe_dump({"cases": [case]}))
    # Reverting the definition must fail the case, naming the gap where there is one.
    metric["expression"]["dialects"][0]["expression"] = fixture["gross_expression"]
    path.write_text(json.dumps(doc))
    result = invoke("mcp", "validate", "--evals", "--json")
    assert result.exit_code == 6
    assert ("gap-refunds" in result.stdout) is ("gap" in fixture)
    if scenario == "empty-model":
        wrong_model = SemanticModel.load(root / "osi", kind="postgres")
        case["expect"]["sql_sha256"] = sql_sha256(wrong_model.compile(request)["sql"])
        (root / "evals/semantic.yaml").write_text(yaml.safe_dump({"cases": [case]}))
        assert invoke("mcp", "validate", "--evals", "--json").exit_code == 0
        (wrong,) = evidence(root, "semantic-3")["matching_cases"]
        assert not wrong["matches_reference_expression"]


def test_semantic_says_next_keeps_refine_until_described_and_keyed():
    assert "until every dataset has a description and a primary key" in load_skill("semantic")


def test_warehouse_seed_distinguishes_aggregate_and_row_subtraction():
    import sqlite3
    from contextlib import closing

    fixture = grader_fixture("empty-model")
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(
            "create table orders(order_id integer, total_amount numeric, "
            "refund_amount numeric, country text)"
        )
        conn.executemany(
            "insert into orders values(:order_id, :total_amount, :refund_amount, :country)",
            fixture["rows"],
        )
        reference, wrong = conn.execute(
            "select sum(total_amount) - sum(refund_amount), "
            "sum(total_amount - refund_amount) from orders"
        ).fetchone()
    assert reference == fixture["expected_revenue"] and wrong != reference
