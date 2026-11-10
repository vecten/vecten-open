import importlib.util
import time
from pathlib import Path

import pytest

from vecten_mcp.admin_service import TOOL_OPERATIONS

spec = importlib.util.spec_from_file_location(
    "usage_tools_builtin_fixture", Path(__file__).with_name("test_builtin_tools.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
builtins = fixture.builtins
invoke, content, rpc = fixture.invoke, fixture.content, fixture.rpc
USAGE_TOOLS = {"usage_view", "search_audit", "read_audit_row", "list_grants", "read_settings"}
MUTATIONS = {"set_gap_status"}


def seed_refusals(builtins):
    now = time.time()
    for index, (reason, offset) in enumerate(
        [("unknown_metric", 1), ("unknown_metric", 2), ("rollup_not_allowed", 3 * 86400)]
    ):
        builtins.runtime.audit.write(
            {
                "id": f"R{index}",
                "at": now - offset,
                "tool": "query_metrics",
                "principal_id": "jane",
                "resolution": "refused",
                "refusal_reason": reason,
                "question": f"Question {index}",
                "request_chain_id": f"R{index}",
            }
        )
    builtins.runtime.audit.write(
        {
            "id": "OLD",
            "at": now - 9 * 86400,
            "tool": "query_metrics",
            "principal_id": "jane",
            "resolution": "refused",
            "refusal_reason": "unknown_metric",
            "request_chain_id": "OLD",
        }
    )


def test_admin_reads_refusals_by_reason_for_the_last_seven_days(builtins):
    fixture.human_admin(builtins)
    seed_refusals(builtins)
    data = content(invoke(builtins, "usage_view", {"view": "refusals", "days": 7}))["data"]
    assert data["days"] == 7
    assert sorted((r["reason"], r["count"]) for r in data["by_day"]) == [
        ("rollup_not_allowed", 1),
        ("unknown_metric", 2),
    ]
    assert {r["question"] for r in data["recent"]} == {"Question 0", "Question 1", "Question 2"}


def test_usage_tools_redact_questions_while_the_toggle_is_on(builtins):
    fixture.human_admin(builtins)
    seed_refusals(builtins)
    builtins.runtime.state.put("settings", "redact", {"value": True})
    data = content(invoke(builtins, "usage_view", {"view": "refusals", "days": 7}))["data"]
    assert {r["question"] for r in data["recent"]} == {"[redacted]"}
    rows = content(invoke(builtins, "search_audit", {"resolution": "refused"}))["data"]
    assert rows and {r["question"] for r in rows} == {"[redacted]"}
    detail = content(invoke(builtins, "read_audit_row", {"row_id": "R0"}))["data"]
    assert detail["row"]["question"] == "[redacted]"


def test_audit_tools_filter_and_follow_chains(builtins):
    fixture.human_admin(builtins)
    seed_refusals(builtins)
    builtins.runtime.audit.write(
        {
            "id": "S0",
            "at": time.time(),
            "tool": "sql-exec",
            "principal_id": "jane",
            "resolution": "agent_sql",
            "request_chain_id": "R0",
        }
    )
    rows = content(invoke(builtins, "search_audit", {"tool": "sql-exec"}))["data"]
    assert [r["id"] for r in rows] == ["S0"]
    detail = content(invoke(builtins, "read_audit_row", {"row_id": "R0"}))["data"]
    assert [r["id"] for r in detail["chain"]] == ["R0", "S0"]
    assert invoke(builtins, "read_audit_row", {"row_id": "missing"})["isError"]


def test_grants_and_settings_are_readable_through_tools(builtins):
    fixture.human_admin(builtins)
    builtins.runtime.state.put("denied", "default", {"columns": ["orders.email"]})
    grants = content(invoke(builtins, "list_grants"))["data"]
    assert grants == {"grants": [], "denied": [{"group": "default", "columns": ["orders.email"]}]}
    settings = content(invoke(builtins, "read_settings"))["data"]
    assert settings["audit_retention_days"] == 90 and settings["redact"] is False


def test_invalid_view_is_an_actionable_tool_error(builtins):
    fixture.human_admin(builtins)
    result = invoke(builtins, "usage_view", {"view": "charts"})
    assert result["isError"]
    assert "view must be one of" in result["content"][0]["text"]


def test_user_client_neither_sees_nor_calls_usage_tools(builtins):
    listed = rpc(builtins.http, builtins.token, "tools/list").json()["result"]["tools"]
    assert not USAGE_TOOLS & {tool["name"] for tool in listed}
    for name in USAGE_TOOLS:
        result = rpc(
            builtins.http, builtins.token, "tools/call", {"name": name, "arguments": {}}
        ).json()
        assert "error" in result or result["result"].get("isError")


def test_admin_tool_list_holds_no_mutation_beyond_gap_status(builtins):
    fixture.human_admin(builtins)
    listed = {
        tool["name"]
        for tool in rpc(builtins.http, builtins.token, "tools/list").json()["result"]["tools"]
    }
    admin_tools = listed - fixture.USER_TOOLS
    assert USAGE_TOOLS <= admin_tools
    writes = {"set_gap_status"}
    assert {name for name in admin_tools if name in writes} == MUTATIONS
    assert not any(
        word in name
        for name in admin_tools
        for word in ("create", "disable", "delete", "grant_", "revoke", "set_admin", "settings_set")
    )
    assert TOOL_OPERATIONS & {"settings/set", "grant", "users/create"} == set()


@pytest.mark.parametrize("days", [0, 3651])
def test_usage_view_days_are_bounded(builtins, days):
    fixture.human_admin(builtins)
    assert invoke(builtins, "usage_view", {"view": "top", "days": days})["isError"]
