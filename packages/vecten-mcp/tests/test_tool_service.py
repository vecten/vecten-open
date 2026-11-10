import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from vecten_mcp.audit import bound_sql_hash, receipt
from vecten_mcp.auth.limits import RateLimit
from vecten_shared.osi.sql_policy import read_statement

spec = importlib.util.spec_from_file_location(
    "service_builtin_fixture", Path(__file__).with_name("test_builtin_tools.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
builtins = fixture.builtins
invoke, content = fixture.invoke, fixture.content


def caller(builtins, *, admin=False, other=False):
    resolved = builtins.runtime.oauth.resolve(builtins.token)
    principal = {**resolved.principal, "kind": "human", "admin": admin}
    if other:
        principal["id"] = "other"
    return SimpleNamespace(principal=principal, groups=resolved.groups)


def report():
    return {
        "question": "Where is churn?",
        "expected": "A metric",
        "returned": "No metric",
        "tool_calls": [{"name": "query_metrics", "arguments": {"metrics": ["churn"]}}],
        "category": "missing metric",
        "raised_by": "agent",
    }


def test_feedback_records_authenticated_identity_and_all_fields(builtins):
    result = content(invoke(builtins, "report_feedback", report()))
    saved = builtins.runtime.state.get("feedback", result["id"])
    assert saved["principal_id"] == builtins.principal["id"]
    assert saved["category"] == "missing metric" and saved["raised_by"] == "agent"
    assert saved["evidence"]["untrusted"] is True
    assert saved["evidence"]["question"] == "Where is churn?"
    assert saved["evidence"]["tool_calls"][0]["type"] == "tool_call"


@pytest.mark.parametrize(("key", "value"), [("category", "other"), ("raised_by", "admin")])
def test_feedback_rejects_invalid_enums(builtins, key, value):
    result = invoke(builtins, "report_feedback", {**report(), key: value})
    assert result["isError"] and not builtins.runtime.state.scan("feedback")
    if key == "category":
        assert "wrong value" in result["content"][0]["text"]


def listed_tool(builtins, name):
    listed = fixture.rpc(builtins.http, builtins.token, "tools/list").json()["result"]["tools"]
    return next(tool for tool in listed if tool["name"] == name)


def test_report_feedback_schema_lists_the_accepted_values(builtins):
    from vecten_mcp.tool_service import CATEGORIES, RAISED_BY

    properties = listed_tool(builtins, "report_feedback")["inputSchema"]["properties"]
    assert properties["category"]["enum"] == list(CATEGORIES)
    assert properties["raised_by"]["enum"] == list(RAISED_BY) == ["user", "agent"]


def test_an_invalid_category_names_the_accepted_values(builtins):
    result = invoke(builtins, "report_feedback", {**report(), "category": "missing_metric"})
    assert result["isError"]
    assert "'missing metric'" in result["content"][0]["text"]


def test_query_metrics_schema_describes_order_by(builtins):
    properties = listed_tool(builtins, "query_metrics")["inputSchema"]["properties"]
    description = properties["order_by"]["description"]
    assert '"dir"' in description and '"asc"' in description and '"field"' in description


def test_order_by_with_direction_names_the_accepted_keys():
    from vecten_shared.osi.compiler import ORDER_KEYS, only_keys

    with pytest.raises(ValueError, match="order_by accepts only: dir, field"):
        only_keys({"field": "orders", "direction": "desc"}, ORDER_KEYS, "order_by")


def test_feedback_rate_limit_is_atomic_and_per_principal(builtins):
    service = builtins.runtime.tool_service
    who = caller(builtins)

    def submit(_):
        try:
            service.report(who, **report())
            return True
        except RateLimit:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = list(pool.map(submit, range(20)))
    assert sum(accepted) == 10
    assert len(builtins.runtime.state.scan("feedback")) == 10
    assert service.report(caller(builtins, other=True), **report())["id"]


def test_feedback_flood_refusals_are_audited(builtins):
    for _ in range(10):
        content(invoke(builtins, "report_feedback", report()))
    result = invoke(builtins, "report_feedback", report())
    assert result["isError"]
    builtins.runtime.audit.flush()
    row = builtins.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])
    assert row["resolution"] == "refused" and row["refusal_reason"] == "rate_limit"


def test_gap_evidence_is_typed_capped_and_status_mutations_are_validated(builtins):
    service = builtins.runtime.tool_service
    builtins.runtime.state.put(
        "gaps",
        "g1",
        {
            "question": "ignore instructions" * 1000,
            "agent_sql": "SELECT " + "x" * 20000,
            "expected": "x" * 5000,
            "returned": "y" * 5000,
            "tool_calls": [{"sql": "z" * 5000}] * 30,
            "category": "wrong value",
            "datasets": ["orders"],
            "columns": ["amount"],
            "occurrence_count": 2,
            "first_seen": 1,
            "last_seen": 2,
            "status": "open",
            "private_internal": "not in contract",
        },
    )
    admin = caller(builtins, admin=True)
    result = service.admin_read(admin, "gaps/read", {"id": "g1"})
    assert result["id"] == "g1" and result["occurrence_count"] == 2
    evidence = result["evidence"]
    assert evidence["untrusted"] is True and len(evidence["question"]) == 4096
    assert len(evidence["agent_sql"]) == 16384
    assert len(evidence["tool_calls"]) == 20
    assert len(evidence["tool_calls"][0]["text"]) == 4096
    assert "private_internal" not in result
    with pytest.raises(ValueError):
        service.set_gap_status(admin, "g1", "invalid")
    with pytest.raises(PermissionError):
        service.set_gap_status(caller(builtins), "g1", "dismissed")
    assert service.set_gap_status(admin, "g1", "dismissed")["status"] == "dismissed"
    assert service.admin_read(admin, "gaps", {}) == []
    assert service.admin_read(admin, "gaps/read", {"id": "g1"})["evidence"] == evidence


def saved_receipt(builtins, *, now=None, principal=None, redacted=False):
    model = builtins.runtime.model.current
    query = {"metrics": ["revenue"]}
    compiled = model.compile(query)
    sql = read_statement(compiled["sql"], kind=model.kind).sql
    stamp = receipt(model.commit, now=now)
    row = {
        "id": stamp["id"],
        "at": stamp["issued_at"],
        "principal_id": principal or builtins.principal["id"],
        "tool": "query_metrics",
        "question": "Sales?",
        "arguments": query,
        "sql": sql,
        "sql_sha256": bound_sql_hash(sql),
        "resolution": "compiled",
        "model_commit": model.commit,
        "redacted": redacted,
    }
    builtins.runtime.audit.write(row)
    return stamp["id"]


def test_receipt_scope_unchanged_and_changed_model(builtins):
    service = builtins.runtime.tool_service
    identifier = saved_receipt(builtins)
    who = caller(builtins)
    original = service.verify(who, identifier)
    assert original["status"] == "found" and original["compiles_identically"] is True
    assert original["question"] == "Sales?" and original["person"] == who.principal["id"]
    assert service.verify(caller(builtins, other=True), identifier) == {"status": "not_found"}
    assert service.verify(caller(builtins, other=True, admin=True), identifier)["status"] == "found"
    path = builtins.osi / "datasets.json"
    path.write_text(path.read_text().replace("SUM(orders.amount)", "AVG(orders.amount)"))
    builtins.runtime.model.reload()
    assert service.verify(who, identifier)["compiles_identically"] is False


def test_receipt_expiry_does_not_disclose_foreign_rows(builtins):
    service = builtins.runtime.tool_service
    expired = saved_receipt(builtins, now=1)
    assert service.verify(caller(builtins), expired) == {"status": "expired"}
    assert service.verify(caller(builtins, other=True), expired) == {"status": "not_found"}
    assert service.verify(caller(builtins), receipt(None, now=1)["id"]) == {"status": "expired"}
    assert service.verify(caller(builtins), receipt(None)["id"]) == {"status": "not_found"}
    with pytest.raises(ValueError):
        service.verify(caller(builtins), "not-a-receipt")


def test_receipt_redaction_and_raw_sql_never_claim_recompile(builtins):
    service = builtins.runtime.tool_service
    identifier = saved_receipt(builtins, redacted=True)
    assert service.verify(caller(builtins), identifier)["compiles_identically"] is None
    builtins.runtime.state.put("settings", "redact", {"value": True})
    result = service.verify(caller(builtins), identifier)
    assert result["question"] == "[redacted]"
    stamp = receipt(None)
    builtins.runtime.audit.write(
        {
            "id": stamp["id"],
            "at": stamp["issued_at"],
            "principal_id": builtins.principal["id"],
            "tool": "sql-exec",
            "sql": "SELECT 1",
        }
    )
    assert service.verify(caller(builtins), stamp["id"])["compiles_identically"] is None


def test_admin_reads_use_safe_shapes_and_aggregate_usage(builtins):
    service = builtins.runtime.tool_service
    admin = caller(builtins, admin=True)
    identifier = saved_receipt(builtins)
    users = service.admin_read(admin, "users", {})
    assert users[0]["kind"] == "service" and "issuer" not in users[0]
    assert service.admin_read(admin, "groups", {})[0]["name"] == "default"
    sessions = service.admin_read(admin, "connected-clients", {})
    assert sessions and "session_id" not in sessions[0]
    assert "token" not in json.dumps(sessions)
    assert service.admin_read(admin, "model-health", {})["status"] == "ready"
    usage = service.admin_read(admin, "usage", {})
    assert usage["questions"] == 1 and usage["by_tool"] == {"query_metrics": 1}
    assert identifier not in json.dumps(usage) and "Sales?" not in json.dumps(usage)
    with pytest.raises(PermissionError):
        service.admin_read(caller(builtins), "users", {})


def test_connected_clients_uses_oauth_clock(builtins):
    state = builtins.runtime.state
    state.put("sessions", "clock-active", {"principal_id": "active", "expires_at": 101})
    state.put("sessions", "clock-expired", {"principal_id": "expired", "expires_at": 100})
    builtins.runtime.oauth.clock = lambda: 100
    rows = builtins.runtime.tool_service.admin_read(
        caller(builtins, admin=True), "connected-clients", {}
    )
    assert "active" in {row["principal_id"] for row in rows}
    assert "expired" not in {row["principal_id"] for row in rows}


def test_usage_aggregates_without_loading_audit_bodies(builtins, monkeypatch):
    from sqlalchemy import event

    service = builtins.runtime.tool_service
    service.clock = lambda: 1000000
    rows = [
        (1000000, "query_metrics", "alice", "compiled", None),
        (1000000, "query_metrics", "alice", "refused", "unknown_metric"),
        (1000000 - 86400, "sql-exec", "bob", "refused", "statement_policy"),
        (1000000 - 86401, "sql-exec", "old", "compiled", None),
        (1000001, "sql-exec", "future", "compiled", None),
        (1000000, "describe", "alice", "compiled", None),
    ]
    for at, tool, principal, resolution, reason in rows:
        builtins.runtime.audit.write(
            {
                "id": receipt(None)["id"],
                "at": at,
                "tool": tool,
                "principal_id": principal,
                "resolution": resolution,
                "refusal_reason": reason,
                "sql": "sensitive SQL",
                "arguments": {"question": "private question"},
            }
        )
    statements = []
    event.listen(
        builtins.runtime.state.engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: statements.append(statement),
    )
    result = service.admin_read(caller(builtins, admin=True), "usage", {"days": 1})
    assert result["questions"] == 3
    assert result["by_tool"] == {"query_metrics": 2, "sql-exec": 1}
    assert result["by_principal"] == {"alice": 2, "bob": 1}
    assert result["refusals"] == {"unknown_metric": 1, "statement_policy": 1}
    audit_reads = [sql for sql in statements if "audit_log" in sql]
    assert audit_reads and all("GROUP BY" in sql for sql in audit_reads)


@pytest.mark.parametrize("cap", [0, 10001, "1000", True])
def test_receipt_recompile_validates_query_cap(builtins, cap):
    identifier = saved_receipt(builtins)
    builtins.runtime.state.put("settings", "row_cap", {"value": cap})
    assert (
        builtins.runtime.tool_service.verify(caller(builtins), identifier)["compiles_identically"]
        is None
    )


def test_feedback_retains_structured_model_context(builtins):
    result = content(
        invoke(
            builtins,
            "report_feedback",
            {
                **report(),
                "tool_calls": [{"name": "query_metrics", "arguments": {"metrics": ["revenue"]}}],
            },
        )
    )
    saved = builtins.runtime.state.get("feedback", result["id"])
    assert saved["names"] == ["revenue"]
    assert saved["datasets"] == ["orders"]


def test_refusal_audit_retains_structured_context(builtins):
    from vecten_mcp.queries import QueryService

    builtins.runtime.queries = QueryService(builtins.runtime.state, None, kind="postgres")
    result = invoke(builtins, "query_metrics", {"metrics": ["unknown"]})
    builtins.runtime.audit.flush()
    saved = builtins.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])
    assert saved["refusal_context"]["metric"] == "unknown"
    assert saved["datasets"] == []


def test_reviewed_fix_updates_provenance_and_next_session_tool_response(builtins, tmp_path):
    import time

    from vecten_mcp.feedback_resolution import FeedbackResolution
    from vecten_mcp.identity import Identity

    runtime = builtins.runtime
    person = runtime.state.create_principal(
        Identity("fixture", "person", "person@example.com", "Person"), "person"
    )
    runtime.state.put("clients", "fixture-client", {"used": False})

    def login():
        with runtime.state.transaction() as conn:
            sid, session = runtime.oauth._session(person["id"], "fixture-client", conn)
            return runtime.oauth._tokens(sid, session, conn)["access_token"]

    builtins.token = login()
    runtime.state.put("gaps", "gap-fix", {"status": "open", "reporters": [person["id"]]})
    runtime.model.commit = lambda: "a" * 40
    runtime.model.reload()
    path = tmp_path / "feedback.json"
    path.write_text(
        json.dumps(
            {
                "commit": "a" * 40,
                "ancestors": [],
                "resolutions": [
                    {
                        "gap": "gap-fix",
                        "merge_commit": "a" * 40,
                        "pull_request": "https://github.com/example/project/pull/1",
                        "author": "writer",
                        "reviewers": ["reviewer"],
                        "merged_at": "2026-10-20T10:00:00Z",
                        "metrics": ["revenue"],
                    }
                ],
            }
        )
    )
    runtime.feedback_resolution = FeedbackResolution(runtime, path, clock=time.time)
    runtime.feedback_resolution.reconcile()
    compiled = runtime.model.current.compile({"metrics": ["revenue"]})
    assert compiled["definitions"]["metrics"]["revenue"]["verified"]["author"] == "writer"
    assert "vecten/definition_changes" not in invoke(builtins, "describe").get("_meta", {})
    builtins.token = login()
    result = invoke(builtins, "describe")
    assert result["_meta"]["vecten/definition_changes"][0]["definitions"] == ["revenue"]
    assert "revenue" in result["content"][-1]["text"]
    assert "vecten/definition_changes" not in invoke(builtins, "describe").get("_meta", {})


def test_sql_feedback_identifies_the_semantic_dataset(builtins):
    result = content(
        invoke(
            builtins,
            "report_feedback",
            {
                **report(),
                "tool_calls": [
                    {
                        "name": "sql-exec",
                        "arguments": {
                            "sql": "SELECT amount FROM marts.orders",
                        },
                    }
                ],
            },
        )
    )
    assert builtins.runtime.state.get("feedback", result["id"])["datasets"] == ["orders"]


def test_notice_delivery_failure_does_not_turn_successful_query_into_error(builtins):
    def unavailable(caller):
        raise RuntimeError("state temporarily unavailable")

    builtins.runtime.feedback_resolution = SimpleNamespace(notices=unavailable)
    result = invoke(builtins, "describe")
    assert not result.get("isError"), result


def test_redaction_covers_report_context_and_preexisting_gap_identifiers(builtins):
    state, service = builtins.runtime.state, builtins.runtime.tool_service
    state.put("settings", "redact", {"value": True})
    result = service.report(caller(builtins), **report())
    saved = state.get("feedback", result["id"])
    assert saved.get("names", []) == []
    assert saved.get("datasets", []) == []
    assert saved.get("columns", []) == []
    view = service.gap_view(
        "gap-1",
        {"names": ["private_name"], "datasets": ["private_table"], "columns": ["private_field"]},
    )
    assert not view["names"] and not view["datasets"] and not view["columns"]


def test_compiled_queries_use_compiler_datasets_without_source_lookup(builtins, monkeypatch):
    fixture.queries(builtins)

    def unexpected(*args):
        raise AssertionError("compiled datasets must bypass source lookup")

    monkeypatch.setattr("vecten_mcp.feedback_signals.table_datasets", unexpected)
    result = content(invoke(builtins, "query_metrics", {"metrics": ["revenue"]}))
    assert result["resolution"] == "compiled"
