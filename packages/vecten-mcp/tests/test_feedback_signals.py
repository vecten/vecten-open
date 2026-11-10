import pytest


def row(identifier="r1", **values):
    return {
        "id": identifier,
        "at": 100,
        "principal_id": "p1",
        "session_id": "s1",
        "request_chain_id": "r1",
        "tool": "query_metrics",
        "resolution": "refused",
        "refusal_reason": "unknown_metric",
        "arguments": {"metrics": ["Churn"]},
        "question": "What is churn?",
        "datasets": [],
        "columns": [],
        **values,
    }


def test_signals_keep_unknown_names_and_capped_untrusted_evidence():
    from vecten_mcp.feedback_signals import audit_signal

    signal = audit_signal(row(question="x" * 5000))
    assert signal["names"] == ["churn"]
    assert signal["reason"] == "unknown_metric"
    assert signal["evidence"]["untrusted"] is True
    assert len(signal["evidence"]["question"]) == 4096
    assert audit_signal(row(arguments="[redacted]"))["names"] == []


@pytest.mark.parametrize(
    "values",
    [
        {"resolution": "compiled", "row_count": 0},
        {"tool": "describe", "resolution": "compiled"},
        {"tool": "describe", "resolution": "error"},
        {"resolution": "error", "error_class": "ValueError"},
        {"tool": "report_feedback", "refusal_reason": "rate_limit"},
    ],
)
def test_normal_results_and_nonwarehouse_errors_are_not_signals(values):
    from vecten_mcp.feedback_signals import audit_signal

    assert audit_signal(row(**values)) is None


def test_warehouse_errors_and_fallback_sql_are_signals():
    from vecten_mcp.feedback_signals import audit_signal

    error = audit_signal(
        row(resolution="error", warehouse_attempted=True, error_class="UndefinedColumn")
    )
    assert error["reason"] == "warehouse_error"
    fallback = audit_signal(row("r2", tool="sql-exec", resolution="agent_sql", sql="SELECT 1"))
    assert fallback["fallback"] is True
    assert fallback["evidence"]["agent_sql"] == "SELECT 1"
    assert (
        audit_signal(row("r3", tool="sql-exec", resolution="agent_sql", request_chain_id="r3"))
        is None
    )


def test_reports_preserve_category_context_and_redaction():
    from vecten_mcp.feedback_signals import report_signal

    signal = report_signal(
        {
            "id": "f1",
            "at": 1,
            "principal_id": "p1",
            "category": "wrong definition",
            "datasets": ["b", "a", "a"],
            "names": ["Revenue"],
            "evidence": {"question": "[redacted]"},
        }
    )
    assert signal["category"] == "wrong definition"
    assert signal["datasets"] == ["a", "b"]
    assert signal["names"] == ["revenue"]
    assert signal["evidence"]["question"] == "[redacted]"


def test_dataset_identifiers_keep_their_case_while_missing_names_are_normalized():
    from vecten_mcp.feedback_signals import report_signal

    signal = report_signal(
        {"id": "f1", "at": 1, "datasets": ["Orders", "orders"], "names": ["CHURN"]}
    )
    assert signal["datasets"] == ["Orders", "orders"]
    assert signal["names"] == ["churn"]


def test_redacted_refusal_does_not_copy_request_identifiers_into_signals():
    from vecten_mcp.audit import redact_row
    from vecten_mcp.feedback_signals import audit_signal

    saved = redact_row(
        row(refusal_context={"field": "private_identifier"}, datasets=["private_identifier"]),
        denied=[],
        kind="postgres",
        redact=True,
    )
    signal = audit_signal(saved)
    assert signal["names"] == []
    assert signal["datasets"] == []
    assert "private_identifier" not in str(saved)


def test_source_matching_requires_unambiguous_qualification_and_caches_parsing(monkeypatch):
    from vecten_mcp.feedback_signals import table_datasets
    from vecten_shared.osi.semantic import SemanticModel

    model = SemanticModel(
        {"raw_orders": {"source": "raw.orders"}, "orders": {"source": "marts.orders"}},
        {},
        {},
        {},
        "postgres",
        "commit",
        {},
    )
    assert table_datasets(["orders"], model) == []
    assert table_datasets(["staging.orders"], model) == []
    assert table_datasets(["marts.orders"], model) == ["orders"]
    # Once the snapshot has been indexed, repeated calls must not parse source SQL.
    from vecten_shared.osi import sql_policy

    def fail(*args, **kwargs):
        raise AssertionError("source SQL parsed on a cached query")

    monkeypatch.setattr(sql_policy, "read_statement", fail)
    assert table_datasets(["raw.orders"], model) == ["raw_orders"]
