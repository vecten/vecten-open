from types import SimpleNamespace

import pytest

from vecten_mcp.state import StateStore


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


@pytest.fixture
def loop(tmp_path):
    from vecten_mcp.audit import AuditService
    from vecten_mcp.feedback import FeedbackService

    state = StateStore("sqlite:///" + str(tmp_path / "feedback.db"))
    runtime = SimpleNamespace(state=state, audit=AuditService(state, start=False))
    yield runtime, FeedbackService(runtime, clock=lambda: 200)
    state.close()


def test_folding_counts_occurrences_once_and_separates_missing_names(loop):
    runtime, service = loop
    for item in [row(), row("r2"), row("r3", arguments={"metrics": ["NET_REVENUE"]})]:
        runtime.audit.write(item)
    assert service.fold() == 3
    assert service.fold() == 0
    gaps = [body for _, body in runtime.state.scan("gaps")]
    assert sorted(g["occurrence_count"] for g in gaps) == [1, 2]
    assert {tuple(g["names"]) for g in gaps} == {("churn",), ("net_revenue",)}


def test_late_fallback_enriches_the_same_occurrence_and_scopes_session(loop):
    runtime, service = loop
    runtime.audit.write(row(refusal_reason="join_required", datasets=["b", "a"]))
    service.fold()
    runtime.audit.write(row("fallback", tool="sql-exec", resolution="agent_sql", sql="SELECT 1"))
    runtime.audit.write(
        row(
            "foreign", session_id="s2", tool="sql-exec", resolution="agent_sql", sql="SELECT secret"
        )
    )
    service.fold()
    gaps = runtime.state.scan("gaps")
    assert len(gaps) == 1
    gap = gaps[0][1]
    assert gap["occurrence_count"] == 1
    assert gap["datasets"] == ["a", "b"]
    assert gap["evidence"]["agent_sql"] == "SELECT 1"


def test_fallback_flushed_before_refusal_is_not_lost(loop):
    runtime, service = loop
    runtime.audit.write(row("fallback", tool="sql-exec", resolution="agent_sql", sql="SELECT 2"))
    service.fold()
    assert runtime.state.scan("gaps") == []
    runtime.audit.write(row())
    service.fold()
    service.fold()
    assert runtime.state.scan("gaps")[0][1]["evidence"]["agent_sql"] == "SELECT 2"


@pytest.mark.parametrize("status", ["open", "triaged", "dismissed", "resolved"])
def test_repeated_report_preserves_admin_status_and_tracks_reporters(loop, status):
    runtime, service = loop
    report = {
        "id": "f1",
        "at": 10,
        "principal_id": "p1",
        "category": "wrong value",
        "datasets": ["orders"],
        "evidence": {"question": "first"},
    }
    runtime.state.put("feedback", "f1", report)
    service.fold()
    identifier, gap = runtime.state.scan("gaps")[0]
    gap["status"] = status
    runtime.state.put("gaps", identifier, gap)
    runtime.state.put("feedback", "f2", {**report, "id": "f2", "at": 20, "principal_id": "p2"})
    service.fold()
    gap = runtime.state.get("gaps", identifier)
    assert gap["status"] == status
    assert gap["occurrence_count"] == 2
    assert gap["first_seen"] == 10 and gap["last_seen"] == 20
    assert gap["reporters"] == ["p1", "p2"]


def test_two_workers_do_not_double_count(loop):
    from concurrent.futures import ThreadPoolExecutor

    from vecten_mcp.feedback import FeedbackService

    runtime, service = loop
    for i in range(20):
        runtime.audit.write(row(str(i)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda s: s.fold(), [service, FeedbackService(runtime)]))
    assert runtime.state.scan("gaps")[0][1]["occurrence_count"] == 20


def test_redaction_applies_to_new_and_existing_gap_reads(loop):
    from vecten_mcp.tool_service import ToolService

    runtime, service = loop
    runtime.state.put("settings", "redact", {"value": True})
    runtime.audit.write(row(question="private question"))
    service.fold()
    identifier, gap = runtime.state.scan("gaps")[0]
    assert gap["evidence"]["question"] == "[redacted]"
    assert ToolService(runtime).gap_view(identifier, gap)["evidence"]["untrusted"]


def test_orphaned_fallbacks_do_not_block_new_audit_rows(loop):
    runtime, service = loop
    for i in range(501):
        runtime.audit.write(
            row(
                f"orphan-{i}",
                request_chain_id=f"missing-{i}",
                tool="sql-exec",
                resolution="agent_sql",
                sql="SELECT 1",
            )
        )
    runtime.audit.write(row("z-real", at=101))
    for _ in range(3):
        service.fold()
    assert len(runtime.state.scan("gaps")) == 1


def test_failure_rolls_back_gap_and_processed_marker(loop, monkeypatch):
    runtime, service = loop
    runtime.audit.write(row())
    put = runtime.state.put

    def unavailable(kind, *args, **kwargs):
        if kind == "feedback_processed":
            raise RuntimeError("storage unavailable")
        return put(kind, *args, **kwargs)

    monkeypatch.setattr(runtime.state, "put", unavailable)
    with pytest.raises(RuntimeError):
        service.fold()
    assert runtime.state.scan("gaps") == []
    monkeypatch.setattr(runtime.state, "put", put)
    assert service.fold() == 1


def test_failed_fallback_enriches_refusal_and_records_warehouse_error(loop):
    runtime, service = loop
    runtime.audit.write(row(refusal_reason="join_required", datasets=["a", "b"]))
    runtime.audit.write(
        row(
            "failed-sql",
            tool="sql-exec",
            resolution="error",
            warehouse_attempted=True,
            sql="SELECT missing FROM a",
        )
    )
    service.fold()
    gaps = [g for _, g in runtime.state.scan("gaps")]
    refusal = next(g for g in gaps if g["reason"] == "join_required")
    assert refusal["evidence"]["agent_sql"] == "SELECT missing FROM a"
    assert any(g["reason"] == "warehouse_error" for g in gaps)


def test_dataset_and_missing_name_do_not_share_a_grouping_namespace(loop):
    runtime, service = loop
    runtime.audit.write(
        row(refusal_reason="unknown_field", datasets=["orders"], refusal_context={"field": "email"})
    )
    runtime.audit.write(
        row("r2", refusal_reason="unknown_field", datasets=[], refusal_context={"field": "orders"})
    )
    service.fold()
    assert len(runtime.state.scan("gaps")) == 2


def test_irrelevant_audit_rows_do_not_allocate_feedback_markers(loop):
    runtime, service = loop
    runtime.audit.write(row("describe", tool="describe", resolution="compiled"))
    assert service.fold() == 0
    assert runtime.state.scan("feedback_processed") == []


def test_processed_markers_live_until_sources_are_retired(loop):
    from sqlalchemy import delete

    from vecten_mcp.state import audit_log

    runtime, service = loop
    runtime.audit.write(row())
    service.fold()
    assert len(runtime.state.scan("feedback_processed")) == 1
    service.clock = lambda: 200 + 91 * 86400
    service.fold()
    assert len(runtime.state.scan("feedback_processed")) == 1
    with runtime.state.transaction() as conn:
        conn.execute(delete(audit_log))
    service.clock = lambda: 200 + 93 * 86400
    service.fold()
    assert runtime.state.scan("feedback_processed") == []
    assert runtime.state.scan("gaps")[0][1]["occurrence_count"] == 1


def test_orphan_fallback_expires_after_retention(loop):
    runtime, service = loop
    runtime.audit.write(row("orphan", tool="sql-exec", resolution="agent_sql", sql="SELECT 1"))
    service.fold()
    assert len(runtime.state.scan("feedback_pending")) == 1
    runtime.state.prune(200 + 91 * 86400)
    assert runtime.state.scan("feedback_pending") == []
