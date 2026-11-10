"""Run browser cookie and CSP enforcement against a loopback-only authorization server."""

import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette

from vecten_mcp.auth.identity import OIDCBackend, PasswordBackend, challenge
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.routes import auth_routes
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.state import StateStore

pytestmark = pytest.mark.browser


@pytest.fixture(params=["chromium", "webkit"])
def browser(request):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = getattr(playwright, request.param).launch()
        yield browser
        browser.close()


@pytest.fixture
def service():
    class Secrets:
        values = {}

        def list_secrets(self):
            return list(self.values)

        def get_secret(self, name):
            return self.values[name]

        def put_secret(self, name, value):
            self.values[name] = value

    state = StateStore("sqlite://")
    state.invite("jane@example.com", "Jane", "jane", provisioned=True)
    backend = PasswordBackend(state)
    backend.set_password(backend.setup_link("jane@example.com"), "long-fixture-password")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    issuer = f"http://127.0.0.1:{listener.getsockname()[1]}"
    service = OAuthService(state, SigningKeys(Secrets(), issuer), issuer, backend)
    server = uvicorn.Server(
        uvicorn.Config(Starlette(routes=auth_routes(service)), log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "test server did not start"
            time.sleep(0.01)
        yield service
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if isinstance(service.backend, OIDCBackend):
            service.backend.client.close()
        state.close()


@pytest.mark.parametrize("decision", ["approve", "deny", "oidc"])
def test_browser_authorization_leaves_form_origin(browser, service, decision):
    callback = "http://127.0.0.1:9000/callback"
    if decision == "oidc":

        def discovery(request):
            assert str(request.url) == "https://idp.example/.well-known/openid-configuration"
            return httpx.Response(
                200,
                json={
                    "issuer": "https://idp.example",
                    "authorization_endpoint": "https://idp.example/authorize",
                    "token_endpoint": "https://idp.example/token",
                    "jwks_uri": "https://idp.example/jwks",
                },
            )

        service.backend = OIDCBackend(
            service.state,
            "https://idp.example",
            "fixture-client",
            service.issuer + "/oidc/callback",
            client=httpx.Client(transport=httpx.MockTransport(discovery)),
        )
    registration = service.register({"redirect_uris": [callback]}, "fixture")
    url = (
        service.issuer
        + "/authorize?"
        + urlencode(
            {
                "client_id": registration["client_id"],
                "redirect_uri": callback,
                "response_type": "code",
                "resource": service.resource,
                "code_challenge": challenge("a" * 43),
                "code_challenge_method": "S256",
                "state": "state<&'\"",
            }
        )
    )
    destination = "https://idp.example/authorize" if decision == "oidc" else callback
    arrivals = []

    def route_request(route):
        request = route.request
        if request.url.startswith(service.issuer + "/"):
            route.continue_()
        elif request.url.startswith(destination + "?"):
            arrivals.append(request)
            route.fulfill(content_type="text/html", body="<p>Authorization destination</p>")
        else:
            route.abort()
            pytest.fail("unexpected browser request outside the local fixtures")

    with browser.new_context() as context:
        page = context.new_page()
        page.set_default_timeout(5000)
        page.route("**/*", route_request)
        try:
            page.goto(url)
            if decision == "oidc":
                page.get_by_role("button", name="Sign in with your provider").click()
            else:
                page.get_by_label("Email").fill("jane@example.com")
                page.get_by_label("Password").fill("long-fixture-password")
                page.get_by_role("button", name="Sign in", exact=True).click()
                page.get_by_role("button", name=decision.capitalize(), exact=True).click()
            page.wait_for_url(destination + "?**", timeout=5000)
            assert len(arrivals) == 1
            assert arrivals[0].method == "GET"
            assert "referer" not in arrivals[0].all_headers()
            query = parse_qs(urlparse(page.url).query)
            if decision == "approve":
                tokens = service.exchange(
                    query["code"][0],
                    registration["client_id"],
                    callback,
                    "a" * 43,
                    service.resource,
                )
                assert (
                    service.resolve(tokens["access_token"]).principal["email"] == "jane@example.com"
                )
            elif decision == "deny":
                assert query["error"] == ["access_denied"] and "code" not in query
            else:
                assert query["client_id"] == ["fixture-client"]
                assert query["code_challenge_method"] == ["S256"]
            if decision != "oidc":
                assert query["state"] == ["state<&'\""]
        finally:
            page.close()
