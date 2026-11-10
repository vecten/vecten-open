"""OAuth 2.1 end to end with a scripted client: what an MCP client and `vecten login` do.

Two MCP deployments share one signing secret store, so a token from one reaching the other is
refused for its audience alone, not for an unknown key.
"""

import importlib.util
import sys
from pathlib import Path

import anyio
import pytest

from vecten.mcp_client import TokenCache, device_complete, device_start, request_admin

SERVED = Path(__file__).parents[2] / "vecten-mcp/tests/served.py"
_spec = importlib.util.spec_from_file_location("scripted_served", SERVED)
served = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = served
_spec.loader.exec_module(served)

A, B = "https://mcp-a.example", "https://mcp-b.example"
PASSWORD = "long-fixture-password"
ADMIN_TOOLS = {"list_gaps", "list_users", "usage", "model_health", "search_audit", "list_grants"}
PERSON_TOOLS = {"describe", "search", "sql-exec", "query_metrics", "report_feedback"}


class Secrets:
    def __init__(self):
        self.values = {}

    def list_secrets(self):
        return list(self.values)

    def get_secret(self, name):
        return self.values[name]

    def put_secret(self, name, value):
        self.values[name] = value


def deployment(issuer, secrets, tmp_path, state=None):
    """One MCP deployment; given another's `state`, it holds that deployment's people,
    sessions and token records instead of its own."""
    from vecten_mcp.access import AccessService
    from vecten_mcp.administration import Administration
    from vecten_mcp.app import create_app
    from vecten_mcp.auth.identity import PasswordBackend
    from vecten_mcp.auth.keys import SigningKeys
    from vecten_mcp.auth.service import OAuthService
    from vecten_mcp.extensions import McpExtensions
    from vecten_mcp.identity import Identity
    from vecten_mcp.runtime import Runtime
    from vecten_mcp.state import StateStore
    from vecten_shared.config import VectenConfig

    shared = state is not None
    if not shared:
        name = issuer.split("//")[1].split(".")[0]
        state = StateStore(f"sqlite:///{tmp_path / name}.db")
    oauth = OAuthService(
        state,
        SigningKeys(secrets, issuer),
        issuer,
        PasswordBackend(state),
        admins=["ada@example.com"],
    )
    config = VectenConfig.model_validate(
        {"project": {"name": "scripted", "scaffold_version": "0.1.0"}, "users": 3}
    )
    runtime = Runtime(state, oauth, McpExtensions.from_config(config), None, None)
    # The admin API is mounted with an administration; its reads need no warehouse.
    runtime.access = AccessService(state, None)
    runtime.administration = Administration(state, None, oauth, runtime.access)
    if shared:
        return create_app(runtime=runtime), runtime
    for email, person in (("grace@example.com", "grace"), ("ada@example.com", "ada")):
        state.invite(email, person.title(), person, provisioned=True)
        oauth.backend.set_password(oauth.backend.setup_link(email), PASSWORD)
    service = state.create_principal(
        Identity("service", "dashboards", "dashboards@service.invalid", "Dashboards"),
        "svc_dashboards",
        kind="service",
        provisioned=True,
    )
    runtime.service_credentials = oauth.service_credentials(service["id"])
    return create_app(runtime=runtime), runtime


@pytest.fixture
def two(tmp_path):
    secrets = Secrets()
    app_a, runtime_a = deployment(A, secrets, tmp_path)
    app_b, runtime_b = deployment(B, secrets, tmp_path)
    with (
        served.TestClient(app_a, base_url=A) as http_a,
        served.TestClient(app_b, base_url=B) as http_b,
    ):
        yield http_a, runtime_a, http_b, runtime_b


@pytest.fixture
def shared(tmp_path):
    """B shares A's signing key and A's state, so B holds every record of A's sessions."""
    secrets = Secrets()
    app_a, runtime_a = deployment(A, secrets, tmp_path)
    app_b, runtime_b = deployment(B, secrets, tmp_path, state=runtime_a.state)
    with (
        served.TestClient(app_a, base_url=A) as http_a,
        served.TestClient(app_b, base_url=B) as http_b,
    ):
        yield http_a, runtime_a, http_b, runtime_b


def sdk_tools(http, issuer, token):
    """The tool names an MCP SDK client lists with `token`."""
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    async def listing():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=http.app),
            headers={"Authorization": "Bearer " + token},
        ) as transport:
            async with Client(
                streamable_http_client(issuer + "/mcp", http_client=transport)
            ) as sdk:
                return {tool.name for tool in (await sdk.list_tools()).tools}

    return anyio.run(listing)


def status(http, token):
    return served.rpc(http, token, "tools/list").status_code


def test_discovery_registration_consent_and_sdk_tool_list(two):
    http, runtime, _, _ = two
    resource = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == A + "/mcp"
    server = http.get("/.well-known/oauth-authorization-server").json()
    assert resource["authorization_servers"] == [server["issuer"]] == [A]
    assert server["registration_endpoint"] == A + "/register"
    assert server["device_authorization_endpoint"] == A + "/device"
    assert server["code_challenge_methods_supported"] == ["S256"]
    unauthenticated = served.rpc(http, None, "tools/list")
    assert unauthenticated.status_code == 401
    assert "resource_metadata" in unauthenticated.headers.get("www-authenticate", "")
    grace = served.human_token(http, resource["resource"], "grace@example.com", PASSWORD)
    tools = sdk_tools(http, A, grace["access_token"])
    assert PERSON_TOOLS <= tools and not tools & ADMIN_TOOLS


def test_refresh_rotates_and_a_replayed_refresh_token_ends_the_session(two):
    http, runtime, _, _ = two
    grace = served.human_token(http, runtime.oauth.resource, "grace@example.com", PASSWORD)
    client_id = grace["client"]["client_id"]
    refreshed = http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": grace["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    fresh = refreshed.json()
    assert fresh["refresh_token"] != grace["refresh_token"]
    assert status(http, fresh["access_token"]) == 200
    replay = http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": grace["refresh_token"],
            "client_id": client_id,
        },
    )
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert status(http, fresh["access_token"]) == 401


def test_revocation_is_immediate(two):
    http, runtime, _, _ = two
    grace = served.human_token(http, runtime.oauth.resource, "grace@example.com", PASSWORD)
    assert status(http, grace["access_token"]) == 200
    revoked = http.post(
        "/revoke",
        data={"token": grace["refresh_token"], "client_id": grace["client"]["client_id"]},
    )
    assert revoked.status_code == 200
    assert status(http, grace["access_token"]) == 401


def test_service_client_credentials_get_the_person_tool_list(two):
    http, runtime, _, _ = two
    creds = runtime.service_credentials
    response = http.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "resource": runtime.oauth.resource,
        },
    )
    assert response.status_code == 200, response.text
    assert "refresh_token" not in response.json()
    tools = sdk_tools(http, A, response.json()["access_token"])
    assert PERSON_TOOLS <= tools and not tools & ADMIN_TOOLS


def test_admin_session_lists_the_admin_tools(two):
    http, runtime, _, _ = two
    ada = served.human_token(http, runtime.oauth.resource, "ada@example.com", PASSWORD)
    assert PERSON_TOOLS | ADMIN_TOOLS <= sdk_tools(http, A, ada["access_token"])


def test_vecten_login_device_flow_reaches_the_admin_api(two, tmp_path):
    http, runtime, _, _ = two
    cache = TokenCache(tmp_path / "home" / "tokens.json")
    public = device_start(A, http, cache)
    assert set(public) >= {"user_code", "verification_uri"} and "device_code" not in public
    browser = served.TestClient(http.app, base_url=A)
    page = browser.get("/device/verify", params={"user_code": public["user_code"]})
    approved = served.browser_consent(browser, page, "ada@example.com", PASSWORD)
    assert approved.status_code in {200, 303}, approved.text[:300]
    device_complete(A, http, cache, sleep=lambda seconds: None)
    token = cache.load(A)["access_token"]
    assert status(http, token) == 200
    users = request_admin(A, "users", cache=cache, http=http)
    assert {"grace@example.com", "ada@example.com"} <= {u["email"] for u in users}


def test_a_token_for_one_resource_server_is_refused_by_another(two):
    http_a, runtime_a, http_b, runtime_b = two
    from_a = served.human_token(http_a, runtime_a.oauth.resource, "grace@example.com", PASSWORD)
    from_b = served.human_token(http_b, runtime_b.oauth.resource, "grace@example.com", PASSWORD)
    assert status(http_a, from_a["access_token"]) == 200
    assert status(http_b, from_b["access_token"]) == 200
    assert status(http_b, from_a["access_token"]) == 401
    assert status(http_a, from_b["access_token"]) == 401


def test_audience_and_issuer_are_each_checked(two):
    import jwt

    http_a, runtime_a, _, runtime_b = two
    token = served.human_token(http_a, runtime_a.oauth.resource, "grace@example.com", PASSWORD)
    token = token["access_token"]
    assert runtime_a.oauth.keys.verify(token, runtime_a.oauth.resource)["aud"] == A + "/mcp"
    # The issuer matches and only the audience differs.
    with pytest.raises(ValueError) as wrong_audience:
        runtime_a.oauth.keys.verify(token, A + "/another-resource")
    assert isinstance(wrong_audience.value.__cause__, jwt.InvalidAudienceError)
    # The audience matches and only the issuer differs.
    with pytest.raises(ValueError) as wrong_issuer:
        runtime_b.oauth.keys.verify(token, runtime_a.oauth.resource)
    assert isinstance(wrong_issuer.value.__cause__, jwt.InvalidIssuerError)


def test_a_server_holding_the_session_still_refuses_a_token_for_another(shared):
    import jwt

    from vecten_mcp.auth.identity import digest

    http_a, runtime_a, http_b, runtime_b = shared
    token = served.human_token(http_a, runtime_a.oauth.resource, "grace@example.com", PASSWORD)
    token = token["access_token"]
    assert status(http_a, token) == 200
    # B can check the signature and finds the access record and the session, so nothing but
    # the audience and issuer checks stands between B and accepting the token.
    kid = jwt.get_unverified_header(token)["kid"]
    assert kid in {key["kid"] for key in runtime_b.oauth.keys.jwks()["keys"]}
    assert runtime_b.oauth.state.get("access", digest(token)) is not None
    assert status(http_b, token) == 401
    with pytest.raises(ValueError, match="^invalid token$"):
        runtime_b.oauth.resolve(token)
