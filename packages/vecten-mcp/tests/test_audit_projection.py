import copy

import pytest

from vecten_mcp.audit import PROJECTION, AuditService, masked
from vecten_mcp.state import StateStore


def row(identifier, at, **values):
    body = {
        "id": identifier,
        "at": at,
        "tool": "query_metrics",
        "principal_id": "jane",
        "resolution": "compiled",
        "refusal_reason": None,
        "metrics": ["revenue"],
        "datasets": ["orders"],
        "columns": ["orders.amount"],
        "question": "What was revenue?",
        "request_chain_id": identifier,
        "arguments": {"metrics": ["revenue"]},
        "sql": "SELECT 1",
        "refusal_context": {"near": "revenue"},
    }
    body.update(values)
    return body


@pytest.fixture
def audit():
    service = AuditService(StateStore("sqlite://"), start=False)
    rows = [
        row("A", 100.0),
        row("B", 200.0, principal_id="jan", tool="sql-exec", resolution="agent_sql"),
        row("C", 300.0, resolution="refused", refusal_reason="unknown_metric"),
        row("D", 301.0, tool="sql-exec", resolution="agent_sql", request_chain_id="C"),
        row("E", 400.0, metrics="not-a-list", datasets=None, columns=None),
    ]
    for body in rows:
        service.write(body)
    yield service
    service.close()


def test_projection_returns_only_named_keys_inside_the_window(audit):
    projected = audit.projection(since=150, until=350)
    assert [item["id"] for item in projected] == ["B", "C", "D"]
    assert all(set(item) == set(PROJECTION) for item in projected)
    assert "sql" not in projected[0] and "arguments" not in projected[0]
    assert projected[1]["refusal_reason"] == "unknown_metric"
    assert projected[0]["columns"] == ["orders.amount"]


def test_projection_turns_malformed_lists_into_empty_lists(audit):
    (item,) = audit.projection(since=400, until=400)
    assert item["metrics"] == [] and item["datasets"] == [] and item["columns"] == []


def test_browse_filters_newest_first_and_pages_with_before(audit):
    assert [r["id"] for r in audit.browse()] == ["E", "D", "C", "B", "A"]
    assert [r["id"] for r in audit.browse(principal_id="jan")] == ["B"]
    assert [r["id"] for r in audit.browse(tool="sql-exec")] == ["D", "B"]
    assert [r["id"] for r in audit.browse(resolution="refused")] == ["C"]
    assert [r["id"] for r in audit.browse(since=200, until=301)] == ["D", "C", "B"]
    assert [r["id"] for r in audit.browse(before=301, limit=2)] == ["C", "B"]
    assert audit.browse()[0]["sql"] == "SELECT 1"


def test_browse_caps_the_page_size(audit):
    for index in range(210):
        audit.write(row(f"X{index:03}", 1000.0 + index))
    assert len(audit.browse(limit=500)) == 200
    with pytest.raises(ValueError):
        audit.browse(limit=0)


def test_chain_rows_follow_a_refusal_to_its_sql_call(audit):
    assert [r["id"] for r in audit.chain_rows("C")] == ["C", "D"]
    assert audit.chain_rows("missing") == []


def test_masked_hides_question_arguments_and_context_without_mutating_input():
    original = row("A", 1.0)
    before = copy.deepcopy(original)
    hidden = masked(original, True)
    assert hidden["question"] == "[redacted]"
    assert hidden["arguments"] == "[redacted]"
    assert hidden["refusal_context"] == {}
    assert hidden["sql"] == "SELECT 1"
    assert original == before
    assert masked(original, False) == original
