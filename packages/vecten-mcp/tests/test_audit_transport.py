import asyncio

# Workspace packages share a tests namespace; load test helpers by their physical path.
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from vecten_mcp import tools as builtin_tools
from vecten_mcp.access import AccessService
from vecten_mcp.app import create_app
from vecten_mcp.audit import AuditService
from vecten_mcp.auth.identity import PasswordBackend
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.extensions import McpExtensions
from vecten_mcp.identity import Identity
from vecten_mcp.runtime import Runtime
from vecten_mcp.state import StateStore
from vecten_mcp.transport import current_request


def helper(module):
    spec = importlib.util.spec_from_file_location(
        "audit_fixture_" + module, Path(__file__).with_name(module + ".py")
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


transport_fixture = helper("test_mcp_transport")
Secrets, TestClient, rpc = (
    transport_fixture.Secrets,
    transport_fixture.TestClient,
    transport_fixture.rpc,
)
query_fixture = helper("test_query_execution").fixture
model_project = helper("test_model_service").project


@pytest.fixture
def audited(tmp_path):
    state = StateStore("sqlite:///" + str(tmp_path / "state.db"))
    oauth = OAuthService(
        state,
        SigningKeys(Secrets(), "https://mcp.example"),
        "https://mcp.example",
        PasswordBackend(state),
    )

    def token(name):
        person = state.create_principal(
            Identity("service", name, name + "@service.invalid", name),
            "svc_" + name,
            kind="service",
            provisioned=True,
        )
        creds = oauth.service_credentials(person["id"])
        return oauth.client_credentials(creds["client_id"], creds["client_secret"], oauth.resource)[
            "access_token"
        ]

    tokens = [token("first"), token("second")]

    def echo(value: str):
        if value == "fail":
            raise ValueError("postgres://user:fixture-secret@host/db")
        if value == "error-result":
            return CallToolResult(content=[TextContent(type="text", text="failed")], is_error=True)
        return {"value": value}

    def metrics(question: str = ""):
        from vecten_mcp.audit_call import current_audit

        current_audit.get().refuse("join_required")
        return {"reason": "join_required"}

    def sql(sql: str, question: str = "", parameters: list | None = None):
        runtime, caller = current_request.get()
        if runtime.queries:
            from vecten_mcp.audit import receipt

            return runtime.queries.execute(caller, sql, parameters or [], receipt("wrong-model"))
        return {"rows": []}

    async def overlap(value: str):
        await asyncio.sleep(0.02)
        return value

    async def cancel():
        raise asyncio.CancelledError()

    tools = {
        name: {"handler": handler, "groups": ["default"]}
        for name, handler in [
            ("echo", echo),
            ("query_metrics", metrics),
            ("sql-exec", sql),
            ("cancel", cancel),
            ("overlap", overlap),
            ("describe", builtin_tools.describe),
        ]
    }
    tools["hidden"] = {"handler": echo, "groups": ["finance"]}
    runtime = Runtime(state, oauth, McpExtensions(tools, {}, {}, {}), None, None)
    runtime.access = AccessService(state, None)
    runtime.audit = AuditService(state, start=False)
    with TestClient(create_app(runtime=runtime), base_url="https://mcp.example") as http:
        yield SimpleNamespace(http=http, runtime=runtime, tokens=tokens)


def invoke(audited, name="echo", arguments=None, token=0):
    return rpc(
        audited.http,
        audited.tokens[token],
        "tools/call",
        {"name": name, "arguments": arguments or {}},
    ).json()["result"]


@pytest.mark.parametrize(
    ("name", "arguments", "resolution"),
    [
        ("echo", {"value": "hello"}, "compiled"),
        ("echo", {"value": "fail"}, "error"),
        ("echo", {"value": "error-result"}, "error"),
        ("echo", {}, "error"),
        ("hidden", {"value": "hidden"}, "refused"),
        ("unknown", {}, "refused"),
    ],
)
def test_every_invocation_has_one_receipt_and_identity_record(audited, name, arguments, resolution):
    result = invoke(audited, name, arguments)
    stamp = result["_meta"]["vecten/receipt"]
    audited.runtime.audit.flush()
    rows = audited.runtime.audit.rows()
    assert len(rows) == 1
    row = rows[0]
    caller = audited.runtime.oauth.resolve(audited.tokens[0])
    assert row["id"] == stamp["id"]
    assert row["principal_id"] == caller.principal["id"]
    assert row["principal_kind"] == "service"
    assert row["client_id"] == caller.client_id and row["session_id"] == caller.session_id
    assert row["tool"] == name and row["resolution"] == resolution
    assert row["plugin"] == (None if name == "unknown" else name)
    assert row["request_chain_id"] == result["_meta"]["vecten/request_chain_id"]
    assert row["duration_ms"] >= 0 and row["at"] == stamp["issued_at"]
    assert "fixture-secret" not in json.dumps(row)
    assert row["model_commit"] is None and row["sql_sha256"] is None
    if arguments.get("value") == "fail":
        assert row["error_class"] == "ValueError"


def test_refusal_chain_is_session_scoped_and_consumed_once(audited):
    refusal = invoke(audited, "query_metrics", {"question": "revenue?"})
    other = invoke(audited, "sql-exec", {"sql": "SELECT 1"}, token=1)
    fallback = invoke(audited, "sql-exec", {"sql": "SELECT 1"})
    independent = invoke(audited, "sql-exec", {"sql": "SELECT 2"})
    chains = [
        r["_meta"]["vecten/request_chain_id"] for r in [refusal, other, fallback, independent]
    ]
    assert chains[0] == chains[2]
    assert len(set(chains)) == 3
    audited.runtime.audit.flush()
    rows = [r for r in audited.runtime.audit.rows() if r["request_chain_id"] == chains[0]]
    assert [r["resolution"] for r in rows] == ["refused", "agent_sql"]
    assert rows[0]["question"] == "revenue?"


def test_database_failure_during_delivery_does_not_hold_http_response(audited, caplog):
    entered, release = threading.Event(), threading.Event()
    audit = audited.runtime.audit

    def unavailable(row):
        entered.set()
        release.wait(5)
        raise OSError("fixture-secret")

    audit.write = unavailable
    try:
        first = invoke(audited, arguments={"value": "one"})
        assert entered.wait(1)
        second = invoke(audited, arguments={"value": "two"})
        assert first["_meta"]["vecten/receipt"]["id"] != second["_meta"]["vecten/receipt"]["id"]
    finally:
        release.set()
    audit.flush()
    assert audit.stats()["dropped"] == 2
    assert "dropped" in caplog.text and "fixture-secret" not in caplog.text


def test_cancellation_is_recorded_and_request_context_is_restored(audited, monkeypatch):
    server = audited.http.app.state.mcp
    monkeypatch.setattr(
        server, "resolved", lambda: audited.runtime.oauth.resolve(audited.tokens[0])
    )

    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await server.call_tool("cancel", {})
        assert current_request.get() is None

    asyncio.run(exercise())
    audited.runtime.audit.flush()
    (row,) = audited.runtime.audit.rows()
    assert row["resolution"] == "error" and row["error_class"] == "CancelledError"


def test_sink_observes_only_persisted_rows(audited):
    audit = audited.runtime.audit
    observed = []
    audit.sink = lambda row: observed.append(audit.find(row["id"]))
    result = invoke(audited, arguments={"value": "sink"})
    audit.flush()
    assert len(observed) == 1
    assert observed[0]["id"] == result["_meta"]["vecten/receipt"]["id"]


def test_concurrent_calls_keep_distinct_receipts_and_caller_identity(audited):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, audited, "overlap", {"value": str(i)}, i) for i in range(2)]
        results = [future.result(timeout=10) for future in futures]
    audited.runtime.audit.flush()
    ids = [result["_meta"]["vecten/receipt"]["id"] for result in results]
    assert len(set(ids)) == 2
    for i, identifier in enumerate(ids):
        row = audited.runtime.audit.find(identifier)
        assert row["arguments"] == {"value": str(i)}
        assert (
            row["principal_id"] == audited.runtime.oauth.resolve(audited.tokens[i]).principal["id"]
        )


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_query_receipt_matches_audit_and_actual_bound_statement(audited, kind):
    import hashlib

    query, conn, cursor, _, _ = query_fixture(kind)
    query.state = audited.runtime.state
    audited.runtime.queries = query
    audited.runtime.model = SimpleNamespace(current=SimpleNamespace(commit="model-commit"))
    audited.runtime.access.set_denied("default", ["customers.email"])
    result = invoke(
        audited,
        "sql-exec",
        {
            "sql": (
                "SELECT c.email, c.status FROM core.customers c "
                "WHERE c.email = %s AND c.status = %s"
            ).replace("%s", "?" if kind == "snowflake" else "%s"),
            "parameters": ["o'brien@example.com", "paid"],
            "question": "Find o'brien@example.com",
        },
    )
    assert not result.get("isError"), result
    stamp = result["_meta"]["vecten/receipt"]
    body = json.loads(result["content"][0]["text"])
    assert body["receipt"] == stamp
    audited.runtime.audit.flush()
    row = audited.runtime.audit.find(stamp["id"])
    assert row["model_commit"] == stamp["model"] == "model-commit"
    assert (
        row["sql_sha256"] == stamp["sql_sha256"] == hashlib.sha256(body["sql"].encode()).hexdigest()
    )
    assert row["tables"] == ["core.customers"]
    assert row["columns"] == ["c.email", "c.status"]
    assert row["row_count"] == 2
    assert "example.com" not in json.dumps(row)
    assert "paid" in row["sql"]
    execute = conn.execute if kind == "postgres" else cursor.execute
    statement = next(
        call.args[0] for call in execute.call_args_list if str(call.args[0]).startswith("/* vecten")
    )
    assert statement.startswith("/* vecten " + stamp["id"] + " */\n")
    assert statement.split("\n", 1)[1] == body["sql_template"]
    assert "LIMIT 1000" in statement


def test_warehouse_error_keeps_sql_evidence_without_error_text(audited):
    query, conn, _, _, _ = query_fixture()
    query.state = audited.runtime.state
    audited.runtime.queries = query

    def execute(statement, parameters=None):
        if str(statement).startswith("/* vecten"):
            raise PermissionError("secret database details")
        return SimpleNamespace()

    conn.execute.side_effect = execute
    result = invoke(audited, "sql-exec", {"sql": "SELECT amount FROM core.orders"})
    assert result["isError"]
    audited.runtime.audit.flush()
    (row,) = audited.runtime.audit.rows()
    assert row["resolution"] == "error" and row["error_class"] == "PermissionError"
    assert row["tables"] == ["core.orders"] and row["columns"] == ["amount"]
    assert row["sql"] == "SELECT amount FROM core.orders LIMIT 1000"
    assert row["sql_sha256"] == result["_meta"]["vecten/receipt"]["sql_sha256"]
    assert row["sql_sha256"] is not None and row["row_count"] is None
    assert "secret database" not in json.dumps(row)


def test_global_redaction_keeps_audit_structure(audited):
    audited.runtime.state.put("settings", "redact", {"value": True})
    result = invoke(audited, "query_metrics", {"question": "private business question"})
    audited.runtime.audit.flush()
    row = audited.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])
    assert row["arguments"] == row["question"] == "[redacted]"
    assert row["resolution"] == "refused" and row["tool"] == "query_metrics"
    assert row["duration_ms"] >= 0


def test_refused_raw_sql_does_not_leak_denied_literals(audited):
    query, _, _, opened, _ = query_fixture()
    query.state = audited.runtime.state
    audited.runtime.queries = query
    audited.runtime.access.set_denied("default", ["customers.email"])
    result = invoke(
        audited,
        "sql-exec",
        {
            "sql": (
                "SELECT email FROM core.customers WHERE email='first'; "
                "SELECT email FROM core.customers WHERE email='second'"
            )
        },
    )
    assert result["isError"] and not opened
    audited.runtime.audit.flush()
    (row,) = audited.runtime.audit.rows()
    assert "first" not in json.dumps(row) and "second" not in json.dumps(row)
    assert row["columns"] == ["email"] and row["tables"] == ["core.customers"]
    assert row["sql_sha256"] is None


def test_semantic_denial_redacts_underlying_physical_column(audited, model_project):
    from vecten_mcp.model import ModelService

    root = model_project
    path = root / "osi/datasets.json"
    document = json.loads(path.read_text())
    document["semantic_model"][0]["datasets"][0]["fields"][1]["name"] = "contact"
    path.write_text(json.dumps(document))
    audited.runtime.model = ModelService(
        root / "osi", audited.runtime.state, kind="postgres", source_exists=lambda *args: True
    )
    audited.runtime.model.reload()
    assert audited.runtime.model.current is not None
    audited.runtime.access.set_denied("default", ["orders.contact"])
    query, _, _, _, _ = query_fixture()
    query.state = audited.runtime.state
    audited.runtime.queries = query
    invoke(
        audited,
        "sql-exec",
        {"sql": "SELECT email FROM marts.orders WHERE email='private@example.com' AND amount=9"},
    )
    audited.runtime.audit.flush()
    (row,) = audited.runtime.audit.rows()
    assert "private@example.com" not in json.dumps(row)
    assert row["columns"] == ["amount", "email"]
    assert "9" in row["sql"]


def test_unavailable_sql_tool_redacts_attempted_query(audited):
    audited.runtime.access.set_denied("default", ["customers.email"])
    audited.runtime.extensions.tools["sql-exec"]["groups"] = ["finance"]
    result = invoke(
        audited,
        "sql-exec",
        {
            "sql": "SELECT email FROM core.customers WHERE email='private@example.com'",
        },
    )
    assert result["isError"]
    audited.runtime.audit.flush()
    (row,) = audited.runtime.audit.rows()
    assert row["resolution"] == "refused"
    assert "private@example.com" not in json.dumps(row)
    assert row["sql_sha256"] is None


@pytest.mark.parametrize("parameters", [7, {"named": "private@example.com"}, "private@example.com"])
def test_invalid_query_parameters_still_have_redacted_audit(audited, parameters):
    result = invoke(
        audited,
        "sql-exec",
        {
            "sql": "SELECT email FROM customers WHERE email=%s",
            "parameters": parameters,
        },
    )
    assert result["isError"]
    audited.runtime.audit.flush()
    (row,) = audited.runtime.audit.rows()
    assert row["resolution"] == "error"
    assert row["arguments"] == "[redacted]" and row["sql_sha256"] is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM customers WHERE email=E'secret' AND status='paid'",
        "SELECT email FROM customers WHERE email=$$secret$$ AND status='paid'",
        "EXPLAIN SELECT email FROM customers WHERE email='secret' AND status='paid'",
        "WITH c(contact) AS (SELECT email FROM customers) "
        "SELECT contact FROM c WHERE contact='secret'",
        "SELECT email FROM customers WHERE email='secret' /* secret */",
    ],
)
@pytest.mark.parametrize("available", [True, False])
def test_review_redaction_at_http_execution_boundary(audited, sql, available, caplog):
    query, _, _, opened, _ = query_fixture()
    query.state = audited.runtime.state
    audited.runtime.queries = query
    audited.runtime.access.set_denied("default", ["customers.email"])
    if not available:
        audited.runtime.extensions.tools["sql-exec"]["groups"] = ["finance"]
    result = invoke(audited, "sql-exec", {"sql": sql, "question": "Find secret"})
    assert bool(result.get("isError")) != available
    assert bool(opened) == available
    audited.runtime.audit.flush()
    (row,) = audited.runtime.audit.rows()
    assert "secret" not in json.dumps(row)
    assert "secret" not in caplog.text
    assert row["id"] == result["_meta"]["vecten/receipt"]["id"]


def test_cancellation_during_preparation_records_once_without_waiting_for_policy(
    audited, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    server = audited.http.app.state.mcp
    monkeypatch.setattr(
        server, "resolved", lambda: audited.runtime.oauth.resolve(audited.tokens[0])
    )

    def blocked_policy(principal):
        entered.set()
        release.wait(5)
        return {"customers.email"}

    monkeypatch.setattr(audited.runtime.access, "denied", blocked_policy)

    async def exercise():
        task = asyncio.create_task(
            server.call_tool(
                "sql-exec", {"sql": "SELECT email FROM customers WHERE email='secret'"}
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert current_request.get() is None
            audited.runtime.audit.flush()
            rows = audited.runtime.audit.rows()
            assert len(rows) == 1
            assert rows[0]["error_class"] == "CancelledError"
            assert rows[0]["arguments"] == "[redacted]"
            assert "secret" not in json.dumps(rows)
        finally:
            release.set()
        await asyncio.sleep(0.05)

    asyncio.run(exercise())
    audited.runtime.audit.flush()
    assert len(audited.runtime.audit.rows()) == 1


def test_redaction_does_not_hold_http_response_and_sink_gets_only_redacted_rows(
    audited, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from vecten_mcp import audit as module

    entered, release = threading.Event(), threading.Event()
    original = module.redact_row

    def slow_redaction(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "redact_row", slow_redaction)
    audited.runtime.access.set_denied("default", ["customers.email"])
    delivered = []
    audited.runtime.audit.sink = delivered.append
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            invoke, audited, "sql-exec", {"sql": "SELECT email FROM customers WHERE email='secret'"}
        )
        try:
            first = future.result(timeout=1)
            assert entered.wait(1)
            second = pool.submit(invoke, audited, "echo", {"value": "hello"}).result(timeout=1)
            assert first["_meta"]["vecten/receipt"]["id"] != second["_meta"]["vecten/receipt"]["id"]
            assert delivered == []
        finally:
            release.set()
    audited.runtime.audit.flush()
    assert len(delivered) == 2
    assert "secret" not in json.dumps(delivered)
    assert "secret" not in json.dumps(audited.runtime.audit.rows())


def test_arguments_that_fail_the_schema_return_the_validation_message(audited):
    result = invoke(audited, "echo", {})
    assert result["isError"]
    text = result["content"][0]["text"]
    assert "value" in text and "Field required" in text


def test_a_tool_without_a_model_says_the_model_is_not_ready(audited, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="vecten_mcp.transport"):
        result = invoke(audited, "describe", {})
    assert result["isError"]
    text = result["content"][0]["text"]
    assert "semantic model is not ready" in text
    receipt = result["_meta"]["vecten/receipt"]["id"]
    assert receipt in caplog.text
