import warnings

import pytest
from sqlalchemy import delete

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
    )
    from starlette.testclient import TestClient

from vecten_mcp.app import create_app
from vecten_mcp.auth.identity import PasswordBackend
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.extensions import McpExtensions
from vecten_mcp.identity import Identity
from vecten_mcp.state import StateStore, memberships


class Secrets:
    def __init__(self):
        self.values = {}

    def list_secrets(self):
        return list(self.values)

    def get_secret(self, name):
        return self.values[name]

    def put_secret(self, name, value):
        self.values[name] = value


@pytest.fixture
def client():
    from vecten_mcp.runtime import Runtime

    state = StateStore("sqlite://")
    state.create_principal(
        Identity("password", "jane@example.com", "jane@example.com", "Jane"),
        "jane",
        provisioned=True,
    )
    keys = SigningKeys(Secrets(), "https://mcp.example")
    oauth = OAuthService(state, keys, "https://mcp.example", PasswordBackend(state))
    calls = []

    def echo(value):
        if value == "fail":
            raise ValueError("postgres://user:fixture-secret@warehouse/db")
        if value == "refused":
            import psycopg.errors

            raise psycopg.errors.InsufficientPrivilege("permission denied for table orders")
        if value == "unprovisioned":
            from vecten_mcp.failures import NoWarehouseAccess

            raise NoWarehouseAccess()
        calls.append(value)
        return value

    extensions = McpExtensions(
        {"echo": {"handler": echo, "groups": ["default"]}},
        {},
        {},
        {},
    )
    runtime = Runtime(state, oauth, extensions, None, None)
    svc = state.create_principal(
        Identity("service", "test", "test@service.invalid", "Test"),
        "svc_test",
        kind="service",
        provisioned=True,
    )
    creds = oauth.service_credentials(svc["id"])
    token = oauth.client_credentials(creds["client_id"], creds["client_secret"], oauth.resource)[
        "access_token"
    ]
    with TestClient(create_app(runtime=runtime), base_url="https://mcp.example") as http:
        yield http, oauth, svc, token, calls


def rpc(http, token, method, params=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
    }
    headers["mcp-method"] = method
    if params and "name" in params:
        headers["mcp-name"] = params["name"]
    if token:
        headers["Authorization"] = "Bearer " + token
    return http.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                **(params or {}),
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )


def test_sdk_serves_authenticated_calls_and_current_groups(client):
    http, oauth, p, token, calls = client
    response = rpc(http, token, "tools/list")
    assert response.status_code == 200, response.text
    assert [tool["name"] for tool in response.json()["result"]["tools"]] == ["echo"]
    result = rpc(http, token, "tools/call", {"name": "echo", "arguments": {"value": "hello"}})
    assert result.status_code == 200 and calls == ["hello"], result.text
    with oauth.state.transaction() as conn:
        conn.execute(delete(memberships).where(memberships.c.principal_id == p["id"]))
    assert rpc(http, token, "tools/list").json()["result"]["tools"] == []
    denied = rpc(http, token, "tools/call", {"name": "echo", "arguments": {"value": "denied"}})
    assert denied.json()["result"]["isError"] and calls == ["hello"]


def test_auth_fails_before_tool_runs_and_sse_is_absent(client):
    http, oauth, p, token, calls = client
    assert rpc(http, None, "tools/list").status_code == 401
    assert rpc(http, "foreign", "tools/list").status_code == 401
    oauth.disable_principal(p["id"])
    assert rpc(http, token, "tools/list").status_code == 401
    assert http.get("/sse").status_code == 404
    assert not calls


def test_tool_failure_is_logged_without_driver_credentials(client, caplog):
    import logging

    http, _, _, token, _ = client
    with caplog.at_level(logging.ERROR, logger="vecten_mcp.transport"):
        response = rpc(http, token, "tools/call", {"name": "echo", "arguments": {"value": "fail"}})
    result = response.json()["result"]
    assert result["isError"]
    assert result["content"][0]["text"] == "Tool execution failed"
    records = [r for r in caplog.records if r.name == "vecten_mcp.transport"]
    assert len(records) == 1
    assert "echo" in records[0].getMessage()
    # The message is logged with the credential redacted, so the cause stays readable.
    assert "ValueError: postgres://[redacted]@warehouse/db" in records[0].getMessage()
    assert records[0].exc_info is None
    assert "fixture-secret" not in caplog.text + response.text


def test_a_warehouse_refusal_reaches_the_client_as_a_reason(client, caplog):
    import logging

    http, _, _, token, _ = client
    with caplog.at_level(logging.ERROR, logger="vecten_mcp.transport"):
        response = rpc(
            http, token, "tools/call", {"name": "echo", "arguments": {"value": "refused"}}
        )
    result = response.json()["result"]
    assert result["isError"]
    assert result["content"][0]["text"] == (
        "the warehouse refused the request (42501): permission denied for table orders"
    )
    assert "InsufficientPrivilege 42501" in caplog.text


def test_chunked_auth_body_is_bounded_before_parsing(client):
    http, _, _, _, _ = client
    response = http.post(
        "/token",
        content=iter([b"x=" + b"a" * 70000]),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413


def test_official_sdk_client_lists_and_calls_over_http(client):
    import anyio
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    http, _, _, token, calls = client

    async def exercise():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=http.app),
            headers={"Authorization": "Bearer " + token},
        ) as transport:
            async with Client(
                streamable_http_client("https://mcp.example/mcp", http_client=transport)
            ) as sdk:
                result = await sdk.list_tools()
                assert [tool.name for tool in result.tools] == ["echo"]
                await sdk.call_tool("echo", {"value": "official-client"})

    anyio.run(exercise)
    assert calls == ["official-client"]


def test_registered_human_consents_then_calls_with_official_sdk(client):
    import re
    from html import unescape
    from urllib.parse import parse_qs, urlparse

    import anyio
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    from vecten_mcp.auth.service import challenge

    http, oauth, _, _, calls = client
    email = "jane@example.com"
    oauth.backend.set_password(oauth.backend.setup_link(email), "long-fixture-password")
    registration = http.post(
        "/register", json={"client_name": "SDK", "redirect_uris": ["http://127.0.0.1:9000/cb"]}
    ).json()
    verifier = "a" * 43
    page = http.get(
        "/authorize",
        params={
            "client_id": registration["client_id"],
            "redirect_uri": registration["redirect_uris"][0],
            "response_type": "code",
            "resource": oauth.resource,
            "code_challenge": challenge(verifier),
            "code_challenge_method": "S256",
            "state": "state",
        },
    )
    flow = re.search(r'name="flow" value="([^"]+)"', page.text)[1]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    consent = http.post(
        "/login",
        data={
            "flow": flow,
            "csrf": csrf,
            "email": email,
            "password": "long-fixture-password",
        },
    )
    assert "Approve" in consent.text
    redirect = http.post(
        "/consent",
        data={
            "flow": flow,
            "csrf": csrf,
            "decision": "approve",
        },
        follow_redirects=False,
    )
    target = unescape(re.search(r'<a href="([^"]+)"', redirect.text)[1])
    code = parse_qs(urlparse(target).query)["code"][0]
    response = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": registration["client_id"],
            "redirect_uri": registration["redirect_uris"][0],
            "code_verifier": verifier,
            "resource": oauth.resource,
        },
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]

    async def exercise():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=http.app),
            headers={"Authorization": "Bearer " + token},
        ) as transport:
            async with Client(
                streamable_http_client("https://mcp.example/mcp", http_client=transport)
            ) as sdk:
                await sdk.call_tool("echo", {"value": "consented-human"})

    anyio.run(exercise)
    assert calls == ["consented-human"]


def test_a_principal_without_warehouse_access_is_told_why(client, caplog):
    import logging

    from vecten_mcp.failures import NoWarehouseAccess

    http, _, _, token, _ = client
    with caplog.at_level(logging.INFO, logger="vecten_mcp.transport"):
        response = rpc(
            http, token, "tools/call", {"name": "echo", "arguments": {"value": "unprovisioned"}}
        )
    result = response.json()["result"]
    assert result["isError"]
    # The SDK names the tool ahead of an anticipated failure's own text.
    assert result["content"][0]["text"] == f"Error executing tool echo: {NoWarehouseAccess()}"
    records = [r for r in caplog.records if r.name == "vecten_mcp.transport"]
    assert [r.levelname for r in records] == ["INFO"]
