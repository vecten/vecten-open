"""The TPC-DS model the live Snowflake suite runs, compiled offline: the SQL that suite executes
is pinned here, so a compiler change shows in review before it reaches the account.

Set VECTEN_UPDATE_GOLDEN=1 to rewrite the golden files after reviewing the change.
"""

import json
import os
from pathlib import Path

import pytest

FIXTURE = Path(__file__).with_name("fixtures") / "tpcds"
CASES = json.loads((FIXTURE / "cases.json").read_text())
GOLDEN = FIXTURE / "golden"


def load_model():
    from vecten_shared.osi.semantic import SemanticModel

    return SemanticModel.load(FIXTURE / "osi", kind="snowflake", commit="fixture")


def test_the_model_validates_with_its_four_relationships():
    model = load_model()
    described = model.describe()
    assert {d["name"] for d in described["datasets"]} == {
        "store_sales",
        "date_dim",
        "customer",
        "item",
        "store",
    }
    assert len(described["relationships"]) == 4
    assert {m["name"] for m in described["metrics"]} == {
        "total_sales",
        "total_profit",
        "net_paid",
        "sales_count",
    }


@pytest.mark.parametrize("case", sorted(CASES["compiled"]))
def test_golden_snowflake_sql(case):
    result = load_model().compile(CASES["compiled"][case], row_cap=1000)
    assert result["resolution"] == "compiled", result
    rendered = f"-- parameters: {json.dumps(result['parameters'])}\n{result['sql']}\n"
    path = GOLDEN / f"{case}.snowflake.sql"
    if os.environ.get("VECTEN_UPDATE_GOLDEN"):
        path.write_text(rendered)
    assert rendered == path.read_text()


@pytest.mark.parametrize("case", sorted(CASES["refused"]))
def test_golden_refusal(case):
    result = load_model().compile(CASES["refused"][case])
    assert result["resolution"] == "refused", result
    golden = {key: result[key] for key in ("reason", "context", "skeleton")}
    path = GOLDEN / f"{case}.refusal.json"
    if os.environ.get("VECTEN_UPDATE_GOLDEN"):
        path.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
    assert golden == json.loads(path.read_text())
