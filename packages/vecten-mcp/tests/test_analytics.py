import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from vecten_mcp.analytics import VIEWS, Analytics, analytics_view
from vecten_mcp.audit import AuditService
from vecten_mcp.state import StateStore
from vecten_shared.osi.semantic import SemanticModel

DAY0 = datetime(2026, 10, 18, 9, tzinfo=UTC).timestamp()
DAY = 86400


def expression(text):
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": text}]}


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    osi = tmp_path_factory.mktemp("osi")
    doc = {
        "version": "0.1.1",
        "semantic_model": [
            {
                "name": "sales",
                "datasets": [
                    {
                        "name": "orders",
                        "source": "marts.orders",
                        "fields": [
                            {
                                "name": "amount",
                                "expression": expression("amount"),
                                "description": "Order amount",
                            },
                            {
                                "name": "email",
                                "expression": expression("email"),
                                "description": "Buyer email",
                                "dimension": {},
                            },
                            {"name": "status", "expression": expression("status"), "dimension": {}},
                        ],
                    },
                    {
                        "name": "customers",
                        "source": "marts.customers",
                        "fields": [
                            {
                                "name": "name",
                                "expression": expression("name"),
                                "description": "Customer name",
                                "dimension": {},
                            },
                            {
                                "name": "segment",
                                "expression": expression("segment"),
                                "description": "  ",
                                "dimension": {},
                            },
                        ],
                    },
                ],
                "metrics": [{"name": "revenue", "expression": expression("SUM(orders.amount)")}],
            }
        ],
    }
    (osi / "datasets.json").write_text(json.dumps(doc))
    return SemanticModel.load(osi)


def audit_row(identifier, at, principal, tool="query_metrics", resolution="compiled", **values):
    row = {
        "id": identifier,
        "at": at,
        "tool": tool,
        "principal_id": principal,
        "resolution": resolution,
        "refusal_reason": None,
        "metrics": [],
        "datasets": [],
        "columns": [],
        "question": "",
        "request_chain_id": identifier,
    }
    row.update(values)
    return row


ROWS = [
    audit_row(
        "R1",
        DAY0,
        "jane",
        metrics=["revenue"],
        datasets=["orders"],
        columns=["orders.amount"],
    ),
    audit_row(
        "R2",
        DAY0 + 60,
        "jane",
        "sql-exec",
        "agent_sql",
        datasets=["orders"],
        columns=['"o"."email"'],
    ),
    audit_row(
        "R3",
        DAY0 + DAY,
        "jan",
        resolution="refused",
        refusal_reason="unknown_metric",
        metrics=["churn"],
        question="What is churn?",
    ),
    audit_row(
        "R4",
        DAY0 + DAY + 60,
        "jan",
        resolution="refused",
        refusal_reason="unknown_metric",
        metrics=["churn"],
        question="Churn by month?",
    ),
    audit_row(
        "R5",
        DAY0 + 2 * DAY,
        "jan",
        resolution="refused",
        refusal_reason="rollup_not_allowed",
        question="Stock by year?",
    ),
    audit_row("R6", DAY0 + 2 * DAY + 10, "jane", "describe", datasets=["customers"]),
    audit_row(
        "R7",
        DAY0 + 2 * DAY + 20,
        "jane",
        "sql-exec",
        "agent_sql",
        datasets=None,
        columns=None,
        metrics=None,
    ),
    audit_row(
        "R8",
        DAY0 + 2 * DAY + 30,
        "jan",
        "sql-exec",
        "agent_sql",
        datasets=["ghost"],
        columns=["amount"],
    ),
    audit_row(
        "R9",
        DAY0 + 2 * DAY + 40,
        "jane",
        metrics=["revenue"],
        datasets=["orders"],
        columns=["amount"],
    ),
]
GAPS = [
    (
        "g-open",
        {"category": "missing metric", "status": "open", "last_seen": 10, "question": "q"},
    ),
    ("g-triaged", {"category": "wrong value", "status": "triaged", "last_seen": 20}),
    ("g-resolved", {"category": "wrong value", "status": "resolved", "last_seen": 30}),
]
NAMES = {"jane": "Jane K", "jan": "Jan N"}


def view(name, model, rows=ROWS, redact=False):
    def gap_view(identifier, row):
        return {"id": identifier, "status": row.get("status", "open"), "evidence": {}, **row}

    return analytics_view(
        name, rows=rows, model=model, gaps=GAPS, principals=NAMES, redact=redact, gap_view=gap_view
    )


def test_questions_count_query_tools_per_utc_day_and_person(model):
    result = view("questions", model)
    assert result["total"] == 8
    assert result["by_day"] == [
        {"day": "2026-10-20", "questions": 4},
        {"day": "2026-10-19", "questions": 2},
        {"day": "2026-10-18", "questions": 2},
    ]
    assert result["rows"] == [
        {"day": "2026-10-20", "principal_id": "jan", "name": "Jan N", "questions": 2},
        {"day": "2026-10-20", "principal_id": "jane", "name": "Jane K", "questions": 2},
        {"day": "2026-10-19", "principal_id": "jan", "name": "Jan N", "questions": 2},
        {"day": "2026-10-18", "principal_id": "jane", "name": "Jane K", "questions": 2},
    ]


def test_top_counts_answered_metrics_and_queried_datasets(model):
    result = view("top", model)
    assert result["metrics"] == [{"name": "revenue", "count": 2}]
    assert result["datasets"] == [{"name": "orders", "count": 3}, {"name": "ghost", "count": 1}]


def test_field_never_in_any_column_list_is_listed_as_unused(model):
    assert view("unused_fields", model)["fields"] == [
        {"dataset": "customers", "field": "name"},
        {"dataset": "customers", "field": "segment"},
        {"dataset": "orders", "field": "status"},
    ]


def test_row_without_datasets_matches_bare_column_names_in_any_dataset(model):
    rows = [audit_row("X", DAY0, "jane", "sql-exec", "agent_sql", columns=['"c".NAME'])]
    unused = view("unused_fields", model, rows)["fields"]
    assert {"dataset": "customers", "field": "name"} not in unused


def test_fields_with_empty_or_blank_descriptions_are_undescribed(model):
    assert view("undescribed_fields", model)["fields"] == [
        {"dataset": "customers", "field": "segment"},
        {"dataset": "orders", "field": "status"},
    ]


def test_refusal_reasons_per_day_and_recent_questions(model):
    result = view("refusals", model)
    assert result["by_day"] == [
        {"day": "2026-10-20", "reason": "rollup_not_allowed", "count": 1},
        {"day": "2026-10-19", "reason": "unknown_metric", "count": 2},
    ]
    assert [r["id"] for r in result["recent"]] == ["R5", "R4", "R3"]
    assert result["recent"][0] == {
        "id": "R5",
        "at": DAY0 + 2 * DAY,
        "reason": "rollup_not_allowed",
        "principal_id": "jan",
        "name": "Jan N",
        "question": "Stock by year?",
    }


def test_refusal_questions_are_redacted_while_the_toggle_is_on(model):
    recent = view("refusals", model, redact=True)["recent"]
    assert {r["question"] for r in recent} == {"[redacted]"}


def test_open_gaps_are_open_or_triaged_newest_first_without_evidence(model):
    gaps = view("open_gaps", model)["gaps"]
    assert [g["id"] for g in gaps] == ["g-triaged", "g-open"]
    assert all("evidence" not in g for g in gaps)


def test_field_usage_covers_every_field_and_metric(model):
    result = view("field_usage", model)
    assert result["fields"] == [
        {"dataset": "customers", "field": "name", "count": 0},
        {"dataset": "customers", "field": "segment", "count": 0},
        {"dataset": "orders", "field": "amount", "count": 2},
        {"dataset": "orders", "field": "email", "count": 1},
        {"dataset": "orders", "field": "status", "count": 0},
    ]
    assert result["metrics"] == [{"name": "revenue", "count": 2}]


def test_views_without_a_model_have_no_fields():
    assert view("unused_fields", None)["fields"] == []
    assert view("field_usage", None) == {"fields": [], "metrics": []}
    assert view("questions", None)["total"] == 8


def test_unknown_view_is_refused(model):
    with pytest.raises(ValueError, match="view must be one of"):
        view("charts", model)


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError("analytics touched the warehouse through " + name)


@pytest.fixture
def runtime(model):
    state = StateStore("sqlite://")
    audit = AuditService(state, start=False)
    for row in ROWS:
        audit.write(row)
    for key, gap in GAPS:
        state.put("gaps", key, gap)
    access = SimpleNamespace(adapter=Forbidden())
    yield SimpleNamespace(
        state=state,
        audit=audit,
        model=SimpleNamespace(current=model),
        pools=Forbidden(),
        queries=Forbidden(),
        access=access,
    )
    audit.close()


def test_every_view_reads_the_state_database_only(runtime):
    service = Analytics(runtime)
    for name in VIEWS:
        result = service.view(name, 30, now=DAY0 + 3 * DAY)
        assert result["view"] == name and result["days"] == 30
        assert result["until"] - result["since"] == 30 * DAY
    assert service.view("questions", 30, now=DAY0 + 3 * DAY)["total"] == 8


def test_view_window_limits_rows(runtime):
    result = Analytics(runtime).view("questions", 1, now=DAY0 + 2 * DAY + 100)
    assert result["total"] == 4


def test_unused_fields_default_to_the_retention_window(runtime):
    runtime.state.put("settings", "audit_retention_days", {"value": 5})
    result = Analytics(runtime).view("unused_fields", None, now=DAY0 + 3 * DAY)
    assert result["days"] == 5
    assert Analytics(runtime).view("top", None, now=DAY0)["days"] == 30


def test_redaction_setting_reaches_the_refusal_view(runtime):
    runtime.state.put("settings", "redact", {"value": True})
    recent = Analytics(runtime).view("refusals", 30, now=DAY0 + 3 * DAY)["recent"]
    assert {r["question"] for r in recent} == {"[redacted]"}


@pytest.mark.parametrize("days", [0, 3651, "7", 1.5, True])
def test_days_must_be_an_integer_within_retention_limits(runtime, days):
    with pytest.raises(ValueError, match="days"):
        Analytics(runtime).view("questions", days)
