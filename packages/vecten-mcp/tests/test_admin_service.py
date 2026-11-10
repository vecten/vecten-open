import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from vecten_mcp.admin_service import (
    READS,
    SETTINGS,
    TOOL_OPERATIONS,
    WRITES,
    AdminRefused,
    AdminService,
    OperationRefused,
)
from vecten_mcp.state import access_events

spec = importlib.util.spec_from_file_location(
    "admin_service_builtin_fixture", Path(__file__).with_name("test_builtin_tools.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
builtins = fixture.builtins


def admin_caller(builtins):
    fixture.human_admin(builtins)
    return builtins.runtime.oauth.resolve(builtins.token)


def events(builtins):
    with builtins.runtime.state.transaction() as conn:
        return [
            (row["operation"], row["subject"], row["outcome"])
            for row in conn.execute(select(access_events).order_by(access_events.c.at)).mappings()
        ]


def test_runtime_builds_one_admin_service(builtins):
    assert isinstance(builtins.runtime.admin_service, AdminService)


def test_service_principal_is_refused_and_recorded(builtins):
    robot = builtins.runtime.oauth.resolve(builtins.token)
    with pytest.raises(AdminRefused):
        builtins.runtime.admin_service.call(robot, "users", {}, surface="api", write=False)
    assert events(builtins)[-1] == ("users", "", "denied")


def test_flagged_admin_is_refused_while_no_enabled_admin_exists(builtins):
    caller = admin_caller(builtins)
    builtins.runtime.state.update_principal(caller.principal["id"], enabled=False)
    with pytest.raises(AdminRefused):
        builtins.runtime.admin_service.call(caller, "users", {}, surface="api", write=False)


def test_reads_and_writes_are_bound_to_their_method(builtins):
    caller = admin_caller(builtins)
    service = builtins.runtime.admin_service
    with pytest.raises(OperationRefused) as read_as_write:
        service.call(caller, "users", {}, surface="api", write=True)
    assert read_as_write.value.status == 404
    with pytest.raises(OperationRefused) as write_as_read:
        service.call(caller, "grant", {}, surface="api", write=False)
    assert write_as_read.value.status == 405
    assert READS.isdisjoint(WRITES)


def test_tool_surface_reaches_only_reads_and_the_gap_status(builtins):
    caller = admin_caller(builtins)
    service = builtins.runtime.admin_service
    assert TOOL_OPERATIONS - READS == {"gaps/status"}
    with pytest.raises(OperationRefused):
        service.call(
            caller, "settings/set", {"key": "redact", "value": True}, surface="mcp", write=True
        )
    assert builtins.runtime.state.get("settings", "redact") is None


def test_each_surface_names_its_access_event(builtins):
    caller = admin_caller(builtins)
    service = builtins.runtime.admin_service
    builtins.runtime.state.put("gaps", "g1", {"status": "open"})
    service.call(caller, "gaps/read", {"id": "g1"}, surface="api", write=False)
    service.call(caller, "gaps/read", {"id": "g1"}, surface="mcp", write=False)
    service.call(
        caller, "gaps/status", {"id": "g1", "status": "triaged"}, surface="panel", write=True
    )
    assert events(builtins)[-3:] == [
        ("gaps/read", "g1", "allowed"),
        ("mcp/gaps/read", "g1", "allowed"),
        ("panel/gaps/status", "g1", "allowed"),
    ]


def test_invalid_request_is_recorded_as_error(builtins):
    caller = admin_caller(builtins)
    with pytest.raises(ValueError):
        builtins.runtime.admin_service.call(
            caller, "gaps/status", {"id": "missing", "status": "triaged"}, surface="api", write=True
        )
    assert events(builtins)[-1] == ("gaps/status", "missing", "error")


def test_settings_list_every_key_with_its_default(builtins):
    caller = admin_caller(builtins)
    service = builtins.runtime.admin_service
    builtins.runtime.state.put("settings", "row_cap", {"value": 500})
    values = service.call(caller, "settings", {}, surface="api", write=False)
    assert values == {
        "audit_retention_days": 90,
        "redact": False,
        "session_idle_seconds": 900,
        "row_cap": 500,
        "statement_timeout_seconds": 30,
    }
    assert set(SETTINGS) == set(values)
    assert all(
        {"default", "minimum", "maximum", "description"} <= set(s) for s in SETTINGS.values()
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("audit_retention_days", 0),
        ("audit_retention_days", 3651),
        ("redact", "yes"),
        ("row_cap", 10001),
        ("statement_timeout_seconds", True),
        ("session_idle_seconds", 29),
        ("unknown", 1),
    ],
)
def test_settings_outside_their_range_are_refused(builtins, key, value):
    caller = admin_caller(builtins)
    with pytest.raises(ValueError):
        builtins.runtime.admin_service.call(
            caller, "settings/set", {"key": key, "value": value}, surface="api", write=True
        )
    assert builtins.runtime.state.get("settings", key) is None


def test_retention_setting_is_saved(builtins):
    caller = admin_caller(builtins)
    builtins.runtime.admin_service.call(
        caller,
        "settings/set",
        {"key": "audit_retention_days", "value": 30},
        surface="panel",
        write=True,
    )
    assert builtins.runtime.state.get("settings", "audit_retention_days") == {"value": 30}


def test_model_read_names_where_definitions_live_and_their_usage(builtins):
    caller = admin_caller(builtins)
    model = builtins.runtime.admin_service.call(caller, "model", {}, surface="api", write=False)
    assert model["semantic_skill"] == "semantic"
    assert model["health"]["status"] == "ready"
    (orders,) = model["datasets"]
    amount = next(f for f in orders["fields"] if f["name"] == "amount")
    assert amount["count"] == 0
    assert amount["described_in"] == "osi/datasets.json: dataset orders, field amount"
    (revenue,) = model["metrics"]
    assert revenue["described_in"] == "osi/metrics.json: metric revenue"
    builtins.runtime.model.platform = True
    model = builtins.runtime.admin_service.call(caller, "model", {}, surface="api", write=False)
    amount = next(f for f in model["datasets"][0]["fields"] if f["name"] == "amount")
    assert (
        amount["described_in"] == "schema.yml under dbt/models/marts/: model orders, column amount"
    )


def test_analytics_and_audit_reads_go_through_the_service(builtins):
    caller = admin_caller(builtins)
    service = builtins.runtime.admin_service
    builtins.runtime.audit.write(
        {
            "id": "R1",
            "at": 10.0,
            "tool": "query_metrics",
            "principal_id": "p",
            "resolution": "refused",
            "refusal_reason": "unknown_metric",
            "question": "What is churn?",
            "request_chain_id": "R1",
        }
    )
    builtins.runtime.audit.write(
        {"id": "R2", "at": 11.0, "tool": "sql-exec", "principal_id": "p", "request_chain_id": "R1"}
    )
    result = service.call(
        caller, "analytics", {"view": "refusals", "days": "7"}, surface="api", write=False
    )
    assert result["view"] == "refusals" and result["days"] == 7
    rows = service.call(caller, "audit", {"tool": "sql-exec"}, surface="api", write=False)
    assert [r["id"] for r in rows] == ["R2"]
    detail = service.call(caller, "audit/read", {"id": "R1"}, surface="api", write=False)
    assert detail["row"]["id"] == "R1"
    assert [r["id"] for r in detail["chain"]] == ["R1", "R2"]
    builtins.runtime.state.put("settings", "redact", {"value": True})
    detail = service.call(caller, "audit/read", {"id": "R1"}, surface="api", write=False)
    assert detail["row"]["question"] == "[redacted]"
    assert service.call(caller, "audit", {}, surface="api", write=False)[1]["question"] == (
        "[redacted]"
    )
    with pytest.raises(ValueError):
        service.call(caller, "audit/read", {"id": "missing"}, surface="api", write=False)


UNREVIEWED_FIX = {
    "pull_request": "https://github.com/example/project/pull/4",
    "merge_commit": "b" * 40,
    "author": "author",
    "merged_at": "2026-10-20T10:00:00Z",
}


def test_admin_confirms_an_unreviewed_fix_on_the_record(builtins):
    caller = admin_caller(builtins)
    state = builtins.runtime.state
    state.put("gaps", "g1", {"status": "open", "unreviewed_fix": UNREVIEWED_FIX})
    service = builtins.runtime.admin_service
    view = service.call(caller, "gaps/confirm", {"id": "g1"}, surface="panel", write=True)
    confirmation = state.get("gaps", "g1")["confirmation"]
    assert confirmation["merge_commit"] == "b" * 40
    assert confirmation["principal_id"] == caller.principal["id"]
    assert confirmation["email"] == caller.principal["email"]
    assert view["confirmation"] == confirmation and view["unreviewed_fix"] == UNREVIEWED_FIX
    assert events(builtins)[-1] == ("panel/gaps/confirm", "g1", "allowed")
    read = service.call(caller, "gaps/read", {"id": "g1"}, surface="api", write=False)
    assert read["confirmation"] == confirmation


@pytest.mark.parametrize(
    "row",
    [
        {"status": "open"},
        {"status": "resolved", "unreviewed_fix": UNREVIEWED_FIX},
    ],
    ids=["no-unreviewed-fix", "resolved"],
)
def test_confirmation_needs_an_unresolved_unreviewed_fix(builtins, row):
    caller = admin_caller(builtins)
    builtins.runtime.state.put("gaps", "g1", row)
    with pytest.raises(ValueError):
        builtins.runtime.admin_service.call(
            caller, "gaps/confirm", {"id": "g1"}, surface="api", write=True
        )
    assert "confirmation" not in builtins.runtime.state.get("gaps", "g1")
    assert events(builtins)[-1] == ("gaps/confirm", "g1", "error")


def test_agents_cannot_confirm_a_fix(builtins):
    caller = admin_caller(builtins)
    builtins.runtime.state.put("gaps", "g1", {"status": "open", "unreviewed_fix": UNREVIEWED_FIX})
    assert "gaps/confirm" in WRITES and "gaps/confirm" not in TOOL_OPERATIONS
    with pytest.raises(OperationRefused):
        builtins.runtime.admin_service.call(
            caller, "gaps/confirm", {"id": "g1"}, surface="mcp", write=True
        )
    assert "confirmation" not in builtins.runtime.state.get("gaps", "g1")
