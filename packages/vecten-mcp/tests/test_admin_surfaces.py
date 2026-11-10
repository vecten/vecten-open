"""One action through any admin surface leaves the same state for every other surface."""

import importlib.util
import re
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from vecten_mcp.state import access_events

spec = importlib.util.spec_from_file_location(
    "surfaces_panel_fixture", Path(__file__).with_name("test_panel_screens.py")
)
screens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screens)
builtins, panel = screens.builtins, screens.panel
fixture = screens.session.fixture
invoke, content, rpc = fixture.invoke, fixture.content, fixture.rpc
ISSUER = "https://mcp.example"


@pytest.fixture
def surfaces(panel, tmp_path):
    from vecten.mcp_client import TokenCache

    runtime = panel.runtime
    runtime.state.put("clients", "cli-client", {"used": True, "client_name": "vecten CLI"})
    with runtime.state.transaction() as conn:
        sid, session = runtime.oauth._session(panel.admin["id"], "cli-client", conn)
        token = runtime.oauth._tokens(sid, session, conn)["access_token"]
    panel.builtins.token = token
    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(
        ISSUER, {"access_token": token, "client_id": "cli-client", "expires_at": time.time() + 3600}
    )
    panel.token, panel.cache = token, cache
    return panel


def api(surfaces, operation, data=None):
    headers = {"Authorization": "Bearer " + surfaces.token}
    if data is None:
        response = surfaces.http.get("/api/admin/" + operation, headers=headers)
    else:
        response = surfaces.http.post("/api/admin/" + operation, headers=headers, json=data)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def api_read(surfaces, operation, **query):
    headers = {"Authorization": "Bearer " + surfaces.token}
    response = surfaces.http.get("/api/admin/" + operation, headers=headers, params=query)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def cli(surfaces, operation, data=None):
    from vecten.mcp_client import request_admin

    return request_admin(ISSUER, operation, data, cache=surfaces.cache, http=surfaces.http)


def tool(surfaces, name, arguments=None):
    return content(invoke(surfaces.builtins, name, arguments))


def events(surfaces):
    with surfaces.runtime.state.transaction() as conn:
        return {row[0] for row in conn.execute(select(access_events.c.operation))}


def test_gap_statuses_set_through_tool_and_panel_read_the_same_everywhere(surfaces):
    state = surfaces.runtime.state
    state.put("gaps", "gap-a", {"status": "open", "category": "wrong value"})
    state.put("gaps", "gap-b", {"status": "open", "category": "missing metric"})
    tool(surfaces, "set_gap_status", {"gap_id": "gap-a", "status": "triaged"})
    screens.post(surfaces, "/admin/gaps/gap-b", {"status": "dismissed"}, origin="/admin/gaps/gap-b")
    expected = {"gap-a": "triaged", "gap-b": "dismissed"}
    through_api = {g["id"]: g["status"] for g in api_read(surfaces, "gaps", status="all")}
    through_tool = {
        g["id"]: g["status"] for g in tool(surfaces, "list_gaps", {"status": "all"})["data"]
    }
    listing = screens.text(surfaces.http.get("/admin/gaps?status=all"))
    through_panel = dict(re.findall(r"(gap-[ab])\s+\S+ \S+\s+.*?(triaged|dismissed)", listing))
    assert through_api == through_tool == through_panel == expected
    assert {"mcp/gaps/status", "panel/gaps/status"} <= events(surfaces)


def test_group_deny_list_and_grant_from_three_surfaces_agree(surfaces):
    cli(surfaces, "groups/create", {"name": "finance"})
    screens.post(surfaces, "/admin/access/denied", {"group": "finance", "columns": "orders.email"})
    api(surfaces, "grant", {"group": "finance", "kind": "dataset", "target": "orders"})
    grant = {"group": "finance", "kind": "dataset", "target": "orders"}
    denied = {"group": "finance", "columns": ["orders.email"]}
    assert grant in cli(surfaces, "grants")
    assert grant in api(surfaces, "grants")
    through_tool = tool(surfaces, "list_grants")["data"]
    assert grant in through_tool["grants"] and denied in through_tool["denied"]
    assert denied in api(surfaces, "denied")
    page = screens.text(surfaces.http.get("/admin/access"))
    assert re.search(r"finance\s+dataset\s+orders", page)
    assert "orders.email" in page
    assert {"groups/create", "panel/denied/set", "grant"} <= events(surfaces)


def test_disabling_through_the_cli_shows_in_the_panel_and_tools(surfaces):
    screens.post(surfaces, "/admin/users/create", {"email": "jane@example.com", "name": "Jane"})
    jane = surfaces.runtime.state.by_email("jane@example.com")
    cli(surfaces, "users/disable", {"id": jane["id"]})
    listed = {u["email"]: u["enabled"] for u in tool(surfaces, "list_users")["data"]}
    assert listed["jane@example.com"] is False
    row = next(
        line
        for line in screens.text(surfaces.http.get("/admin/users")).splitlines()
        if "jane@example.com" in line
    )
    assert "disabled" in row


def test_the_agent_finds_no_mutation_tool(surfaces):
    listed = {
        t["name"]
        for t in rpc(surfaces.http, surfaces.token, "tools/list").json()["result"]["tools"]
    }
    assert listed >= {"list_users", "list_grants", "usage_view"}
    assert not {
        name
        for name in listed
        if any(word in name for word in ("disable", "create", "delete", "grant_", "revoke"))
    }
    assert "set_gap_status" in listed
