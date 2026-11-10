"""Request rules, golden SQL and refusals of the single-dataset metric compiler."""

import json
import os
from pathlib import Path

import pytest

FIXTURE = Path(__file__).with_name("fixtures") / "compiler"
CASES = json.loads((FIXTURE / "cases.json").read_text())
GOLDEN = FIXTURE / "golden"


def load_fixture(kind="postgres"):
    from vecten_shared.osi.semantic import SemanticModel

    return SemanticModel.load(FIXTURE / "osi", kind=kind, commit="fixture")


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ({"metrics": []}, "metrics must be a nonempty list of names"),
        ({"metrics": ["total_revenue", "total_revenue"]}, "metrics must be unique"),
        ({"dimensions": "country"}, "dimensions must be a list"),
        ({"dimensions": [1]}, "field name must be a string"),
        ({"dimensions": ["country", "country"]}, "output names must be unique"),
        ({"time": "month"}, "time must be an object"),
        (
            {"time": {"grain": "month", "from": "2026-01-01"}},
            "time accepts only: dimension, end, grain, start",
        ),
        ({"time": {"grain": "hour"}}, "time grain must be one of: day, week, month, quarter, year"),
        ({"time": {"dimension": "order_date"}}, "time needs a grain, a start or an end"),
        (
            {"time": {"grain": "month", "start": "last month"}},
            "time.start must be an ISO date or timestamp",
        ),
        (
            {"time": {"grain": "month", "start": "2026-13-01"}},
            "time.start must be an ISO date or timestamp",
        ),
        (
            {"time": {"grain": "month", "end": 20260101}},
            "time.end must be an ISO date or timestamp",
        ),
        ({"filters": {"field": "status"}}, "filters must be a list"),
        ({"filters": ["status = paid"]}, "filter must be an object"),
        (
            {"filters": [{"field": "status", "operator": "=", "value": "paid"}]},
            "filter accepts only: field, op, value",
        ),
        (
            {"filters": [{"field": "status", "op": "regex", "value": "p"}]},
            "filter operator must be one of: =, equals, !=, not equals, >, >=, <, <=, like, in, "
            "between, is null",
        ),
        ({"filters": [{"field": "status", "op": "in", "value": "paid"}]}, "in needs a list"),
        (
            {"filters": [{"field": "amount", "op": "between", "value": [1, 2, 3]}]},
            "between needs two values",
        ),
        (
            {"filters": [{"field": "status", "op": "=", "value": ["paid"]}]},
            "filter values must be scalar",
        ),
        ({"order_by": {"field": "total_revenue"}}, "order_by must be a list"),
        (
            {"order_by": [{"field": "total_revenue", "direction": "desc"}]},
            "order_by accepts only: dir, field",
        ),
        (
            {"order_by": [{"field": "total_revenue", "dir": "down"}]},
            "order direction must be asc or desc",
        ),
        ({"limit": 0}, "limit must be a positive integer"),
        ({"limit": True}, "limit must be a positive integer"),
        ({"limit": "10"}, "limit must be a positive integer"),
    ],
)
def test_request_rule_errors(fragment, message):
    with pytest.raises(ValueError) as raised:
        load_fixture().compile({"metrics": ["total_revenue"], **fragment})
    assert str(raised.value).startswith(message)


def test_time_range_without_grain_filters_without_grouping():
    result = load_fixture().compile(
        {"metrics": ["total_revenue"], "time": {"start": "2026-01-01", "end": "2027-01-01"}}
    )
    assert result["resolution"] == "compiled", result
    assert "DATE_TRUNC" not in result["sql"] and "GROUP BY" not in result["sql"]
    assert "order_date >= %s" in result["sql"] and "order_date < %s" in result["sql"]
    assert result["parameters"] == ["2026-01-01", "2027-01-01"]


def test_time_dimension_defaults_to_the_only_one():
    result = load_fixture().compile({"metrics": ["total_revenue"], "time": {"grain": "month"}})
    assert "DATE_TRUNC('month', order_date) AS \"order_date\"" in result["sql"]


@pytest.mark.parametrize(
    ("limit", "cap", "expected"), [(None, 50, 50), (5000, 50, 50), (10, 50, 10)]
)
def test_row_cap_applies_with_and_without_limit(limit, cap, expected):
    query = {"metrics": ["total_revenue"]} | ({"limit": limit} if limit else {})
    assert load_fixture().compile(query, row_cap=cap)["sql"].endswith(f"LIMIT {expected}")


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
@pytest.mark.parametrize("case", sorted(CASES["compiled"]))
def test_golden_sql(case, kind):
    from sqlglot import exp

    from vecten_shared.osi.sql_policy import read_statement

    result = load_fixture(kind).compile(CASES["compiled"][case], row_cap=1000)
    assert result["resolution"] == "compiled", result
    rendered = f"-- parameters: {json.dumps(result['parameters'])}\n{result['sql']}\n"
    path = GOLDEN / f"{case}.{kind}.sql"
    if os.environ.get("VECTEN_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(rendered)
    assert rendered == path.read_text()
    tree = read_statement(result["sql"], kind=kind).tree
    assert len(list(tree.find_all(exp.Placeholder))) == len(result["parameters"])
    for value in result["parameters"]:
        assert not isinstance(value, str) or value not in result["sql"]


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_derived_source_carries_the_dataset_alias(kind):
    query = {"metrics": ["account_balance"], "time": {"grain": "month"}}
    sql = load_fixture(kind).compile(query)["sql"]
    assert "'test%') AS \"balances\" GROUP BY" in sql
    refused = load_fixture(kind).compile({"metrics": ["account_balance"]})
    assert refused["skeleton"].endswith("'test%') AS \"balances\"")


def test_ambiguous_time_dimension_names_the_candidates():
    result = load_fixture().compile({"metrics": ["event_count"], "time": {"grain": "day"}})
    assert result["context"] == {
        "field": "time.dimension",
        "candidates": ["created_at", "processed_at"],
    }


def test_denied_mixed_case_time_dimension_is_neither_a_candidate_nor_counted(tmp_path):
    import shutil

    from vecten_shared.osi.semantic import SemanticModel

    shutil.copytree(FIXTURE / "osi", tmp_path / "osi")
    path = tmp_path / "osi" / "sales.json"
    path.write_text(path.read_text().replace('"processed_at"', '"ProcessedAt"'))
    model = SemanticModel.load(tmp_path / "osi", kind="postgres", commit="fixture")
    query = {"metrics": ["event_count"], "time": {"grain": "day"}}

    defaulted = model.compile(query, denied={"events.ProcessedAt"})
    assert defaulted["resolution"] == "compiled", defaulted
    assert "DATE_TRUNC('day', created_at)" in defaulted["sql"]

    refused = model.compile(query, denied={"events.ProcessedAt", "events.created_at"})
    assert refused["context"] == {"field": "time.dimension", "candidates": []}


REASONS = {
    "join_required_dimension": "join_required",
    "join_required_unqualified": "join_required",
    "join_required_metrics": "join_required",
    "unknown_metric": "unknown_metric",
    "unknown_field": "unknown_field",
    "unknown_field_fact_as_dimension": "unknown_field",
    "unknown_field_time_default": "unknown_field",
    "unknown_field_order_by": "unknown_field",
    "ambiguous_dataset": "ambiguous_dataset",
    "rollup_not_allowed_no_rule": "rollup_not_allowed",
    "rollup_not_allowed_no_grain": "rollup_not_allowed",
}


@pytest.mark.parametrize(
    ("asked", "denied", "suggested", "hidden"),
    [
        ("turnovr", set(), "total_revenue", None),
        ("gross sale", set(), "total_revenue", None),
        ("markets", set(), "country", None),
        ("markets", {"orders.country"}, None, "country"),
    ],
)
def test_unknown_metric_suggests_through_synonyms(asked, denied, suggested, hidden):
    result = load_fixture().compile({"metrics": [asked]}, denied=denied)
    assert result["reason"] == "unknown_metric"
    suggestions = result["context"]["suggestions"]
    assert suggested is None or suggestions[0] == suggested
    assert hidden not in suggestions
    assert len(suggestions) == len(set(suggestions))


@pytest.mark.parametrize("case", sorted(CASES["refused"]))
def test_golden_refusals(case):
    result = load_fixture().compile(CASES["refused"][case])
    assert result["resolution"] == "refused", result
    assert result["sql"] is None and result["parameters"] == []
    assert result["reason"] == REASONS[case]
    golden = {key: result[key] for key in ("reason", "context", "skeleton")}
    path = GOLDEN / "refusals" / f"{case}.json"
    if os.environ.get("VECTEN_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
    assert golden == json.loads(path.read_text())


def test_every_refusal_reason_has_a_golden():
    assert set(REASONS) == set(CASES["refused"])
    assert set(REASONS.values()) | {"dialect_missing"} == {
        "join_required",
        "unknown_metric",
        "unknown_field",
        "ambiguous_dataset",
        "dialect_missing",
        "rollup_not_allowed",
    }


def test_definition_without_the_warehouse_dialect_is_refused():
    # Validation refuses such a model before it loads; this reaches the compiler's own guard.
    model = load_fixture("postgres")
    channel = next(f for f in model.datasets["orders"]["fields"] if f["name"] == "channel")
    channel["expression"]["dialects"] = [
        d for d in channel["expression"]["dialects"] if d["dialect"] == "SNOWFLAKE"
    ]
    result = model.compile({"metrics": ["total_revenue"], "dimensions": ["channel"]})
    assert result["reason"] == "dialect_missing"
    assert result["context"] == {"definition": "channel", "dialect": "postgres"}
    assert result["sql"] is None


def test_metric_referencing_a_metric_on_another_dataset_needs_a_join():
    # Validation refuses such a model before it loads; this reaches the compiler's own guard.
    model = load_fixture()
    model.metrics["revenue_per_customer"] = {
        **model.metrics["total_revenue"],
        "name": "revenue_per_customer",
        "expression": {
            "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount) / customer_count"}]
        },
    }
    model.homes["revenue_per_customer"] = "orders"
    result = model.compile({"metrics": ["revenue_per_customer"]})
    assert result["reason"] == "join_required"
    assert result["context"]["datasets"] == ["orders", "customers"]
    assert result["context"]["references"] == "customer_count"
    assert "JOIN marts.customers" in result["skeleton"]
