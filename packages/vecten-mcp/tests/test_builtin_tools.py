import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.administration import Administration
from vecten_mcp.app import create_app
from vecten_mcp.audit import AuditService
from vecten_mcp.auth.identity import PasswordBackend
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.extensions import McpExtensions
from vecten_mcp.identity import Identity
from vecten_mcp.model import ModelService
from vecten_mcp.runtime import Runtime
from vecten_mcp.state import StateStore
from vecten_shared.config import VectenConfig
from vecten_shared.extensions import selected_entry_points
from vecten_shared.output import Failure

spec = importlib.util.spec_from_file_location(
    "builtin_transport_fixture", Path(__file__).with_name("test_mcp_transport.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
Secrets, TestClient, rpc = fixture.Secrets, fixture.TestClient, fixture.rpc
USER_TOOLS = {
    "describe",
    "search",
    "sql-exec",
    "query_metrics",
    "report_feedback",
    "verify_receipt",
}
ADMIN_TOOLS = {
    "list_gaps",
    "read_gap",
    "set_gap_status",
    "list_users",
    "list_groups",
    "usage",
    "model_health",
    "connected_clients",
    "usage_view",
    "search_audit",
    "read_audit_row",
    "list_grants",
    "read_settings",
}


def config():
    return VectenConfig.model_validate(
        {"project": {"name": "demo", "scaffold_version": "0.1.0"}, "users": 2}
    )


def test_builtin_entry_points_and_group_named_admin_do_not_grant_admin():
    extensions = McpExtensions.from_config(config())
    assert set(extensions.tools) == USER_TOOLS | ADMIN_TOOLS
    assert set(extensions.tools_for({"default", "admin"})) == USER_TOOLS
    assert set(extensions.tools_for(set())) == USER_TOOLS
    assert set(extensions.tools_for({"default"}, admin=True)) == USER_TOOLS | ADMIN_TOOLS
    assert {extensions.plugin_for(name) for name in extensions.tools} == {"vecten-mcp"}


def test_plugin_cannot_replace_builtin():
    points = [
        SimpleNamespace(name="describe", dist=SimpleNamespace(name=name, version="0.1.0"))
        for name in ("vecten-mcp", "custom")
    ]
    configured = config().model_copy(update={"extensions": ["custom"]})
    with pytest.raises(Failure, match="duplicate"):
        selected_entry_points(configured, "vecten.mcp.tools", points)


@pytest.fixture
def builtins(tmp_path):
    state = StateStore("sqlite:///" + str(tmp_path / "state.db"))
    oauth = OAuthService(
        state,
        SigningKeys(Secrets(), "https://mcp.example"),
        "https://mcp.example",
        PasswordBackend(state),
    )
    extensions = McpExtensions.from_config(config())
    runtime = Runtime(state, oauth, extensions, None, None)
    runtime.audit = AuditService(state, start=False)
    runtime.access = AccessService(state, None)
    runtime.administration = Administration(state, None, oauth, runtime.access)
    osi = tmp_path / "osi"
    osi.mkdir()

    def expression(text):
        return {"dialects": [{"dialect": "ANSI_SQL", "expression": text}]}

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
                                "ai_context": {"synonyms": ["turnover"]},
                            },
                            {"name": "email", "expression": expression("email"), "dimension": {}},
                        ],
                    }
                ],
                "metrics": [{"name": "revenue", "expression": expression("SUM(orders.amount)")}],
            }
        ],
    }
    (osi / "datasets.json").write_text(json.dumps(doc))
    runtime.model = ModelService(osi, state, kind="postgres", source_exists=lambda table: True)
    principal = state.create_principal(
        Identity("service", "robot", "robot@service.invalid", "Robot"),
        "svc_robot",
        kind="service",
        provisioned=True,
    )
    credentials = oauth.service_credentials(principal["id"])
    token = oauth.client_credentials(
        credentials["client_id"], credentials["client_secret"], oauth.resource
    )["access_token"]
    with TestClient(create_app(runtime=runtime), base_url="https://mcp.example") as http:
        yield SimpleNamespace(runtime=runtime, http=http, token=token, principal=principal, osi=osi)


def invoke(builtins, name, arguments=None):
    return rpc(
        builtins.http, builtins.token, "tools/call", {"name": name, "arguments": arguments or {}}
    ).json()["result"]


def content(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def test_service_sees_user_tools_and_cannot_call_admin_even_with_admin_flag(builtins):
    listed = rpc(builtins.http, builtins.token, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == USER_TOOLS
    builtins.runtime.state.update_principal(builtins.principal["id"], admin=True)
    assert invoke(builtins, "list_users")["isError"]
    assert invoke(builtins, "set_gap_status", {"gap_id": "fake", "status": "resolved"})["isError"]


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_discovery_uses_synonyms_and_omits_denied_columns(builtins, kind):
    builtins.runtime.model.kind = kind
    builtins.runtime.model.reload()
    result = content(invoke(builtins, "describe", {"dataset": "orders"}))
    assert result["datasets"][0]["fields"][0]["classification"] == "fact"
    assert result["metrics"][0]["name"] == "revenue"
    assert (
        content(invoke(builtins, "search", {"query": "turnover"}))["results"][0]["name"] == "amount"
    )
    builtins.runtime.state.put("denied", "default", {"columns": ["orders.amount"]})
    result = content(invoke(builtins, "describe", {"dataset": "orders"}))
    assert [f["name"] for f in result["datasets"][0]["fields"]] == ["email"]
    assert result["metrics"] == []
    assert content(invoke(builtins, "search", {"query": "turnover"}))["results"] == []


def queries(builtins, kind="postgres"):
    spec = importlib.util.spec_from_file_location(
        "builtin_query_fixture", Path(__file__).with_name("test_query_execution.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    query, conn, cursor, opened, _ = module.fixture(kind)
    query.state = builtins.runtime.state
    builtins.runtime.queries = query
    builtins.runtime.model.kind = kind
    builtins.runtime.model.reload()
    return conn, cursor, opened


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_sql_tool_executes_as_caller_and_returns_matching_receipt(builtins, kind):
    conn, cursor, opened = queries(builtins, kind)
    result = invoke(builtins, "sql-exec", {"sql": "SELECT amount FROM marts.orders"})
    data = content(result)
    assert data["rows"] == [[7], [8]]
    assert data["receipt"] == result["_meta"]["vecten/receipt"]
    assert opened[0][0]["id"] == builtins.principal["id"]
    assert opened[0][3] == {"analyst"}
    builtins.runtime.audit.flush()
    row = builtins.runtime.audit.find(data["receipt"]["id"])
    assert row["plugin"] == "vecten-mcp"
    assert row["tables"] == ["marts.orders"] and row["columns"] == ["amount"]
    assert row["sql_sha256"] == data["receipt"]["sql_sha256"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT 1; SELECT 2",
        "SELECT * INTO other FROM orders",
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "EXPLAIN ANALYZE SELECT 1",
        "EXPLAIN (ANALYZE FALSE) SELECT 1",
    ],
)
def test_sql_tool_rejects_unsafe_statements_without_opening_connection(builtins, sql):
    _, _, opened = queries(builtins)
    result = invoke(builtins, "sql-exec", {"sql": sql})
    assert result["isError"]
    assert not opened
    builtins.runtime.audit.flush()
    row = builtins.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])
    assert row["resolution"] == "refused"


def test_metrics_compile_execute_and_refusal_chain(builtins):
    _, _, opened = queries(builtins)
    result = content(
        invoke(builtins, "query_metrics", {"metrics": ["revenue"], "question": "Sales?"})
    )
    assert result["resolution"] == "compiled"
    assert result["operations"]["revenue"]["additivity"] == "additive"
    assert "revenue" in result["definitions"]["metrics"]
    assert len(opened) == 1
    refused = invoke(builtins, "query_metrics", {"metrics": ["missing"]})
    assert content(refused)["reason"] == "unknown_metric"
    assert len(opened) == 1
    fallback = invoke(builtins, "sql-exec", {"sql": "SELECT amount FROM marts.orders"})
    assert (
        refused["_meta"]["vecten/request_chain_id"] == fallback["_meta"]["vecten/request_chain_id"]
    )


def test_sdk_client_refusal_then_agent_sql_share_a_chain(builtins):
    import anyio
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    queries(builtins)

    async def exercise():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=builtins.http.app),
            headers={"Authorization": "Bearer " + builtins.token},
        ) as transport:
            async with Client(
                streamable_http_client("https://mcp.example/mcp", http_client=transport)
            ) as sdk:
                refused = await sdk.call_tool("query_metrics", {"metrics": ["turnover"]})
                body = json.loads(refused.content[0].text)
                assert body["reason"] == "unknown_metric"
                assert body["receipt"]["sql_sha256"] is None
                fallback = await sdk.call_tool(
                    "sql-exec", {"sql": "SELECT SUM(amount) FROM marts.orders"}
                )
                assert not fallback.is_error
                return refused.meta, fallback.meta

    refused, fallback = anyio.run(exercise)
    assert refused["vecten/request_chain_id"] == fallback["vecten/request_chain_id"]
    builtins.runtime.audit.flush()
    rows = [
        builtins.runtime.audit.find(meta["vecten/receipt"]["id"]) for meta in (refused, fallback)
    ]
    assert [row["resolution"] for row in rows] == ["refused", "agent_sql"]
    assert rows[0]["refusal_reason"] == "unknown_metric"
    assert rows[0]["request_chain_id"] == rows[1]["request_chain_id"]


def test_denied_metric_never_executes(builtins):
    _, _, opened = queries(builtins)
    builtins.runtime.state.put("denied", "default", {"columns": ["orders.amount"]})
    result = content(invoke(builtins, "query_metrics", {"metrics": ["revenue"]}))
    assert result["resolution"] == "refused"
    assert not opened


def test_timeout_has_receipt_and_timeout_audit_class_without_driver_message(builtins):
    conn, _, _ = queries(builtins)
    conn.execute.side_effect = TimeoutError("sensitive driver diagnostics")
    result = invoke(builtins, "sql-exec", {"sql": "SELECT 1"})
    assert result["isError"]
    assert "sensitive" not in json.dumps(result)
    builtins.runtime.audit.flush()
    row = builtins.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])
    assert row["error_class"] == "timeout"


def human_admin(builtins):
    state, oauth = builtins.runtime.state, builtins.runtime.oauth
    person = state.create_principal(
        Identity("password", "admin@example.com", "admin@example.com", "Admin"),
        "human_admin",
        admin=True,
        provisioned=True,
    )
    state.put("clients", "human-client", {"used": True})
    with state.transaction() as conn:
        sid, session = oauth._session(person["id"], "human-client", conn)
        token = oauth._tokens(sid, session, conn)["access_token"]
    builtins.token = token
    return person


def test_human_admin_lists_calls_and_loses_tools_immediately_when_flag_removed(builtins):
    person = human_admin(builtins)
    listed = rpc(builtins.http, builtins.token, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == USER_TOOLS | ADMIN_TOOLS
    assert content(invoke(builtins, "model_health"))["data"]["status"] == "ready"
    assert content(invoke(builtins, "list_gaps"))["data"] == []
    users = content(invoke(builtins, "list_users"))["data"]
    assert {user["kind"] for user in users} == {"human", "service"}
    assert content(invoke(builtins, "list_groups"))["data"][0]["name"] == "default"
    assert content(invoke(builtins, "usage"))["data"]["questions"] == 0
    assert content(invoke(builtins, "connected_clients"))["data"]
    builtins.runtime.state.update_principal(person["id"], admin=False)
    assert invoke(builtins, "list_users")["isError"]
    listed = rpc(builtins.http, builtins.token, "tools/list").json()["result"]["tools"]
    assert {tool["name"] for tool in listed} == USER_TOOLS


def test_admin_api_and_tools_share_gap_evidence_and_status(builtins):
    human_admin(builtins)
    builtins.runtime.state.put("gaps", "gap", {"question": "Untrusted data", "status": "open"})
    headers = {"Authorization": "Bearer " + builtins.token}
    response = builtins.http.get("/api/admin/gaps/read?id=gap", headers=headers)
    assert response.status_code == 200, response.text
    data = content(invoke(builtins, "read_gap", {"gap_id": "gap"}))["data"]
    assert response.json()["data"] == data
    assert data["evidence"]["untrusted"] is True
    changed = builtins.http.post(
        "/api/admin/gaps/status", headers=headers, json={"id": "gap", "status": "triaged"}
    )
    assert changed.status_code == 200, changed.text
    assert content(invoke(builtins, "read_gap", {"gap_id": "gap"}))["data"]["status"] == "triaged"
    assert (
        content(invoke(builtins, "set_gap_status", {"gap_id": "gap", "status": "dismissed"}))[
            "status"
        ]
        == "dismissed"
    )
    assert builtins.http.get("/api/admin/gaps", headers=headers).json()["data"] == []


def test_verifies_real_parameterized_metric_receipt_without_warehouse_execution(builtins):
    _, _, opened = queries(builtins)
    result = content(
        invoke(
            builtins,
            "query_metrics",
            {
                "metrics": ["revenue"],
                "filters": [{"field": "email", "op": "=", "value": "a@example.com"}],
            },
        )
    )
    builtins.runtime.audit.flush()
    stamp = result["receipt"]
    verified = content(invoke(builtins, "verify_receipt", {"receipt_id": stamp["id"]}))
    assert verified["compiles_identically"] is True
    assert len(opened) == 1


def test_cancellation_waits_for_bounded_query_before_finalizing_audit(builtins, monkeypatch):
    import asyncio
    import threading

    import anyio

    from vecten_mcp.transport import AuthorizedMCP

    conn, cursor, _ = queries(builtins)
    entered, release = threading.Event(), threading.Event()

    def execute(sql, *args):
        if str(sql).startswith("/* vecten"):
            entered.set()
            assert release.wait(5)
        return cursor

    conn.execute.side_effect = execute
    server = AuthorizedMCP(builtins.runtime)
    who = builtins.runtime.oauth.resolve(builtins.token)
    monkeypatch.setattr(server, "resolved", lambda: who)

    async def exercise():
        task = asyncio.create_task(server.call_tool("sql-exec", {"sql": "SELECT 1"}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0.03)
            assert not task.done(), "audit finalized while warehouse query was still running"
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    anyio.run(exercise)
    builtins.runtime.audit.flush()
    row = builtins.runtime.audit.rows()[-1]
    assert row["row_count"] == 2 and row["sql_sha256"]
    assert row["error_class"] == "CancelledError"


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_denied_dependencies_and_dataset_context_are_absent_from_discovery(builtins, kind):
    _, _, opened = queries(builtins, kind)
    path = builtins.osi / "datasets.json"
    doc = json.loads(path.read_text())
    model = doc["semantic_model"][0]
    dataset = model["datasets"][0]
    dataset["source"] = "SELECT amount, email FROM marts.orders"
    dataset["ai_context"] = {"synonyms": ["private email addresses"]}
    dataset["fields"].append(
        {
            "name": "normalized_email",
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "LOWER(email)"}]},
        }
    )
    model["metrics"].append(
        {
            "name": "contact_count",
            "expression": {
                "dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(orders.normalized_email)"}
                ]
            },
        }
    )
    path.write_text(json.dumps(doc))
    builtins.runtime.model.reload()
    builtins.runtime.state.put("denied", "default", {"columns": ["orders.email"]})
    described = content(invoke(builtins, "describe"))
    assert "email" not in json.dumps(described).casefold()
    assert [m["name"] for m in described["metrics"]] == ["revenue"]
    assert content(invoke(builtins, "search", {"query": "email"}))["results"] == []
    assert (
        content(invoke(builtins, "query_metrics", {"metrics": ["contact_count"]}))["resolution"]
        == "refused"
    )
    assert not opened
    # Query provenance still describes exactly the SQL that executed; discovery is a projection.
    allowed = content(invoke(builtins, "query_metrics", {"metrics": ["revenue"]}))
    assert "email" in allowed["definitions"]["source"]
    assert builtins.runtime.model.current.datasets["orders"]["source"] == dataset["source"]


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_unknown_metric_suggestions_do_not_reveal_mixed_case_denied_fields(builtins, kind):
    queries(builtins, kind)
    path = builtins.osi / "datasets.json"
    doc = json.loads(path.read_text())
    model = doc["semantic_model"][0]
    model["datasets"][0]["name"] = "Orders"
    model["datasets"][0]["fields"][1]["name"] = "Email"
    model["metrics"][0]["expression"]["dialects"][0]["expression"] = "SUM(Orders.amount)"
    path.write_text(json.dumps(doc))
    builtins.runtime.model.reload()
    builtins.runtime.state.put("denied", "default", {"columns": ["oRdErS.eMaIl"]})
    result = content(invoke(builtins, "query_metrics", {"metrics": ["Emai"]}))
    assert result["reason"] == "unknown_metric"
    assert "Email" not in result["context"]["suggestions"]


def test_admin_tool_actions_also_write_access_events(builtins):
    from sqlalchemy import select

    from vecten_mcp.state import access_events

    person = human_admin(builtins)
    builtins.runtime.state.put("gaps", "g1", {"question": "Question", "status": "open"})
    content(invoke(builtins, "set_gap_status", {"gap_id": "g1", "status": "triaged"}))
    content(invoke(builtins, "read_gap", {"gap_id": "g1"}))
    with builtins.runtime.state.transaction() as conn:
        events = [
            dict(row)
            for row in conn.execute(
                select(access_events).where(access_events.c.operation.like("mcp/%"))
            ).mappings()
        ]
    assert {e["operation"] for e in events} == {"mcp/gaps/status", "mcp/gaps/read"}
    assert all(
        e["principal_id"] == person["id"] and e["subject"] == "g1" and e["outcome"] == "allowed"
        for e in events
    )


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_denying_derived_field_keeps_its_inputs_visible_and_queryable(builtins, kind):
    queries(builtins, kind)
    path = builtins.osi / "datasets.json"
    doc = json.loads(path.read_text())
    dataset = doc["semantic_model"][0]["datasets"][0]
    dataset["description"] = "Orders with amount"
    dataset["fields"].extend(
        {
            "name": name,
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": expression}]},
        }
        for name, expression in [("is_large", "amount > 1000"), ("large_flag", "is_large")]
    )
    path.write_text(json.dumps(doc))
    builtins.runtime.model.reload()
    builtins.runtime.state.put("denied", "default", {"columns": ["orders.is_large"]})
    described = content(invoke(builtins, "describe"))
    assert [f["name"] for f in described["datasets"][0]["fields"]] == ["amount", "email"]
    assert described["datasets"][0]["description"] == "Orders with amount"
    assert [m["name"] for m in described["metrics"]] == ["revenue"]
    assert content(invoke(builtins, "search", {"query": "amount"}))["results"]
    assert content(invoke(builtins, "query_metrics", {"metrics": ["revenue"]}))["rows"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"filters": [{"field": "amount", "op": ["="], "value": 1}]}, "filter operator"),
        ({"order_by": [{"field": "revenue", "dir": ["asc"]}]}, "order direction"),
    ],
)
def test_malformed_query_options_are_actionable_refusals(builtins, arguments, message):
    _, _, opened = queries(builtins)
    result = invoke(builtins, "query_metrics", {"metrics": ["revenue"], **arguments})
    assert result["isError"] and message in result["content"][0]["text"]
    assert not opened
    builtins.runtime.audit.flush()
    row = builtins.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])
    assert row["resolution"] == "refused" and row["refusal_reason"] == "invalid_request"


def test_discovery_reuses_parsed_snapshot_dependencies(builtins, monkeypatch):
    import vecten_shared.osi.semantic as semantic

    model = builtins.runtime.model.current
    model.describe(denied={"orders.email"})

    def unexpected_parse(*args, **kwargs):
        pytest.fail("discovery reparsed an immutable snapshot expression")

    monkeypatch.setattr(semantic, "selected_expression", unexpected_parse)
    assert model.describe()["metrics"][0]["name"] == "revenue"
    assert model.search("turnover", denied={"orders.email"})
    assert "revenue" in model.suggestions("revenu", {"orders.email"})
