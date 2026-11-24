"""The sign-in's browser cookies on mcp.<domain> cannot be set by a sibling hostname.

Any hostname under <domain> can set a cookie with `Domain=<domain>`, and the server receives
it as a plain `name=value` pair. A browser refuses a `__Host-` cookie that names a Domain, so
on HTTPS the server reads only `__Host-` names; a cookie of any other name is ignored, and a
planted one is sent here the way the server would see it, as a `Cookie` header.
"""

import asyncio
import re
import warnings
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.requests import Request

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
    )
    from starlette.testclient import TestClient

from vecten_mcp.auth.gate import GateRoute, GateService
from vecten_mcp.auth.identity import OIDCBackend, PasswordBackend, challenge, cookie_value, digest
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.routes import auth_routes
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.identity import Identity
from vecten_mcp.state import StateStore

ISSUER = "https://mcp.data.acme.com"
PORTAL = "portal.data.acme.com"
PASSWORD = "long-fixture-password"
REDIRECT = "http://127.0.0.1:9000/callback"


class Secrets:
    def __init__(self):
        self.values = {}

    def list_secrets(self):
        return list(self.values)

    def get_secret(self, name):
        return self.values[name]

    def put_secret(self, name, value):
        self.values[name] = value


class Provider(OIDCBackend):
    """The identity provider is simulated: every return signs in as the victim, Ada."""

    def metadata(self):
        return {"authorization_endpoint": "https://idp.example/authorize"}

    def callback(self, state, code):
        pending = self.state.take("oidc_states", digest(state), now=self.clock())
        if not pending or code != "provider-code":
            raise ValueError("OIDC state is invalid or expired")
        return Identity("https://idp.example", "ada-sub", "ada@acme.com", "Ada"), pending["flow"]


class Deployment:
    def __init__(self, issuer, *, oidc=False):
        self.state = StateStore("sqlite://")
        self.ada = self.state.invite("ada@acme.com", "Ada", "ada", provisioned=True)
        password = PasswordBackend(self.state)
        password.set_password(password.setup_link("ada@acme.com"), PASSWORD)
        backend = (
            Provider(self.state, "https://idp.example", "client", issuer + "/oidc/callback",
                     client=object())
            if oidc
            else password
        )  # fmt: skip
        self.oauth = OAuthService(self.state, SigningKeys(Secrets(), issuer), issuer, backend)
        self.gate = GateService(
            self.oauth, {"acme-portal.web.home": GateRoute(PORTAL, ("default",))}
        )
        self.app = Starlette(routes=auth_routes(self.oauth, self.gate))
        self.issuer = issuer
        self.client = self.oauth.register(
            {"client_name": "Agent", "redirect_uris": [REDIRECT]}, "ip"
        )

    def browser(self, base_url=None):
        return TestClient(self.app, base_url=base_url or self.issuer)

    def authorize(self, http):
        return http.get(
            "/authorize",
            params=dict(
                client_id=self.client["client_id"],
                redirect_uri=REDIRECT,
                code_challenge=challenge("a" * 43),
                code_challenge_method="S256",
                response_type="code",
                resource=self.oauth.resource,
                state="state",
            ),
        )

    def flow_principal(self, flow):
        return (self.state.get("flows", digest(flow)) or {}).get("principal_id")


def hidden(page, name):
    return unescape(re.search(rf'name="{name}" value="([^"]+)"', page.text)[1])


def set_cookies(response):
    return {line.split("=", 1)[0]: line.lower() for line in response.headers.get_list("set-cookie")}


@pytest.fixture
def https():
    deployment = Deployment(ISSUER)
    yield deployment
    deployment.state.close()


@pytest.fixture
def oidc():
    deployment = Deployment(ISSUER, oidc=True)
    yield deployment
    deployment.state.close()


def started_with_the_provider(deployment, attacker):
    """The attacker's own flow, csrf value and the provider state its sign-in returns with."""
    page = deployment.authorize(attacker)
    flow, csrf = hidden(page, "flow"), hidden(page, "csrf")
    provider = attacker.post("/oidc/start", data={"flow": flow, "csrf": csrf})
    assert provider.status_code == 200, provider.text
    target = urlsplit(unescape(re.search(r'<a href="([^"]+)"', provider.text)[1]))
    return flow, csrf, parse_qs(target.query)["state"][0]


def raw_get(app, path, query, cookie):
    """The status of a GET whose Cookie header carries exactly these bytes; the test client
    re-encodes bytes outside ASCII, so the ASGI app is called directly."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [(b"host", b"mcp.data.acme.com"), (b"cookie", cookie)],
        "server": ("mcp.data.acme.com", 443),
        "client": ("203.0.113.9", 50000),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


@pytest.mark.parametrize(
    "name",
    [
        b"vecten_csrf",
        b"__Secure-vecten_csrf",
        b"__host-vecten_csrf",
        # Unprefixed to a browser, which takes a name byte for byte, but a parser that strips
        # latin-1 whitespace from names reads __Host-vecten_csrf.
        b"\xa0__Host-vecten_csrf",
        b"\x85__Host-vecten_csrf",
    ],
)
def test_a_csrf_cookie_planted_by_a_sibling_does_not_finish_anothers_sign_in(oidc, name):
    with oidc.browser() as attacker:
        flow, csrf, state = started_with_the_provider(oidc, attacker)
    query = f"state={state}&code=provider-code"
    assert raw_get(oidc.app, "/oidc/callback", query, name + b"=" + csrf.encode()) == 400
    assert oidc.flow_principal(flow) is None


def test_the_browser_that_started_the_sign_in_finishes_it(oidc):
    with oidc.browser() as person:
        flow, _, state = started_with_the_provider(oidc, person)
        returned = person.get(f"/oidc/callback?state={state}&code=provider-code")
    assert returned.status_code == 200 and "Approve" in returned.text
    assert oidc.flow_principal(flow) == oidc.ada["id"]


def test_https_sign_in_cookies_are_host_only(https):
    with https.browser() as http:
        page = https.authorize(http)
        signed = http.post(
            "/login",
            data=dict(flow=hidden(page, "flow"), csrf=hidden(page, "csrf"),
                      email="ada@acme.com", password=PASSWORD),
        )  # fmt: skip
        assert signed.status_code == 200, signed.text
        clients = http.get("/connected-clients")
        assert clients.status_code == 200
        setup = http.get("/set-password")
    sent = set_cookies(page) | set_cookies(signed) | set_cookies(clients) | set_cookies(setup)
    assert set(sent) == {
        "__Host-vecten_csrf",
        "__Host-vecten_browser",
        "__Host-vecten_clients_csrf",
        "__Host-vecten_setup_csrf",
    }
    for line in sent.values():
        assert "; secure" in line and "; path=/;" in line + ";" and "domain=" not in line


def test_a_password_sign_in_refuses_a_csrf_cookie_of_another_name(https):
    with https.browser() as attacker, https.browser() as victim:
        page = https.authorize(attacker)
        flow, csrf = hidden(page, "flow"), hidden(page, "csrf")
        refused = victim.post(
            "/login",
            data=dict(flow=flow, csrf=csrf, email="ada@acme.com", password=PASSWORD),
            headers={"Cookie": f"vecten_csrf={csrf}"},
        )
    assert refused.status_code == 400
    assert https.flow_principal(flow) is None


def test_a_planted_browser_session_does_not_sign_the_gate_in(https):
    mallory = https.state.invite("mallory@acme.com", "Mallory", "mallory", provisioned=True)
    https.state.put(
        "browser_sessions", digest("mallory-browser"), {"principal_id": mallory["id"]},
        expires_at=10**10,
    )  # fmt: skip
    with https.browser() as victim:
        asked = victim.get(
            "/gate/authorize",
            params={"host": PORTAL, "challenge": challenge("v" * 43), "rd": "/"},
            headers={"Cookie": "vecten_browser=mallory-browser"},
            follow_redirects=False,
        )
    # Sent to sign in rather than handed a code for the planter.
    assert asked.status_code == 200 and 'name="flow"' in asked.text
    assert https.state.scan("gate_codes") == []


def test_connected_clients_and_password_setup_refuse_a_planted_token(https):
    https.state.put(
        "browser_sessions", digest("ada-browser"), {"principal_id": https.ada["id"]},
        expires_at=10**10,
    )  # fmt: skip
    session = "vecten_browser=ada-browser; __Host-vecten_browser=ada-browser"
    with https.browser() as victim:
        revoke = victim.post(
            "/connected-clients",
            data={"csrf": "planted", "client_id": https.client["client_id"]},
            headers={"Cookie": session + "; vecten_clients_csrf=planted"},
            follow_redirects=False,
        )
        token = https.oauth.backend.setup_link("ada@acme.com")
        setup = victim.post(
            "/set-password",
            data={"csrf": "planted", "token": token, "password": "another-long-password"},
            headers={"Cookie": "vecten_setup_csrf=planted"},
        )
    assert revoke.status_code == 400
    assert setup.status_code == 400


@pytest.mark.parametrize(
    "issuer,base_url",
    [
        # A server whose issuer is plain HTTP, and an HTTPS one reached through a loopback tunnel.
        ("http://mcp.local:8000", None),
        (ISSUER, "http://127.0.0.1:8001"),
    ],
)
def test_plain_http_signs_in_with_unprefixed_cookies(issuer, base_url):
    deployment = Deployment(issuer)
    try:
        with deployment.browser(base_url) as http:
            page = deployment.authorize(http)
            signed = http.post(
                "/login",
                data=dict(flow=hidden(page, "flow"), csrf=hidden(page, "csrf"),
                          email="ada@acme.com", password=PASSWORD),
            )  # fmt: skip
            assert signed.status_code == 200 and "Approve" in signed.text
            approved = http.post(
                "/consent",
                data=dict(flow=hidden(signed, "flow"), csrf=hidden(signed, "csrf"),
                          decision="approve"),
            )  # fmt: skip
            assert approved.status_code == 200 and "code=" in approved.text
    finally:
        deployment.state.close()
    sent = set_cookies(page) | set_cookies(signed)
    assert set(sent) == {"vecten_csrf", "vecten_browser"}
    assert all("; secure" not in line for line in sent.values())


@pytest.mark.parametrize(
    "origin,forwarded",
    [
        ("http://mcp.example", {}),
        (
            "http://mcp.example",
            {
                "Forwarded": "host=localhost;proto=http",
                "X-Forwarded-Host": "127.0.0.1",
                "X-Forwarded-Proto": "http",
            },
        ),
        ("https://127.0.0.1", {}),
    ],
)
def test_https_issuer_selects_host_cookie_outside_a_plain_loopback_tunnel(https, origin, forwarded):
    with https.browser(origin) as http:
        http.headers.update(forwarded)
        sent = set_cookies(https.authorize(http))
    assert set(sent) == {"__Host-vecten_csrf"}
    assert "; secure" in sent["__Host-vecten_csrf"]


@pytest.mark.parametrize(
    "headers",
    [
        [(b"cookie", b"__Host-vecten_csrf=first; __Host-vecten_csrf=second")],
        [(b"cookie", b"__Host-vecten_csrf=first"), (b"cookie", b"__Host-vecten_csrf=second")],
    ],
)
def test_ambiguous_prefixed_cookie_has_no_value(headers):
    request = Request({"type": "http", "headers": headers})
    assert cookie_value(request, "__Host-vecten_csrf") == ""
