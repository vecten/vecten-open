"""The gate: sign-in for service hostnames, one-time codes, and per-request verification.

Requests to /gate/start, /gate/callback, /gate/logout and /gate/verify are sent the way NGINX
on a service hostname forwards them: with X-Vecten-Gate-Host and only that host's cookies.
"""

import re
import warnings
from html import unescape
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, update
from starlette.applications import Starlette

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
    )
    from starlette.testclient import TestClient

from vecten_mcp.auth.gate import GateRoute, GateService
from vecten_mcp.auth.identity import OIDCBackend, PasswordBackend, challenge, digest
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.routes import auth_routes
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.identity import Identity
from vecten_mcp.state import StateStore, gate_sessions, groups, memberships, principals
from vecten_shared.config import VectenConfig

ISSUER = "https://mcp.data.acme.com"
PORTAL = "portal.data.acme.com"
DASH = "dash.data.acme.com"
HOME = "acme-portal.web.home"
BOOKS = "acme-portal.web.books"
DASH_HOME = "acme-dash.app.home"
ROUTES = {
    HOME: GateRoute(PORTAL, ("default",)),
    BOOKS: GateRoute(PORTAL, ("finance",)),
    DASH_HOME: GateRoute(DASH, ("default",)),
}
PASSWORD = "long-fixture-password"


class Secrets:
    def __init__(self):
        self.values = {}

    def list_secrets(self):
        return list(self.values)

    def get_secret(self, name):
        return self.values[name]

    def put_secret(self, name, value):
        self.values[name] = value


class Browser:
    """One browser: the server's own pages through `mcp`, service hostnames through NGINX."""

    def __init__(self, app):
        self.mcp = TestClient(app, base_url=ISSUER)
        self.front = TestClient(app, base_url=ISSUER)
        self.jars: dict[str, dict[str, str]] = {}
        self.set_cookies: dict[str, list[str]] = {}

    def service(self, host, path, *, method="GET", headers=None, cookies=None):
        jar = self.jars.setdefault(host, {}) if cookies is None else cookies
        sent = {"X-Vecten-Gate-Host": host, **(headers or {})}
        if jar:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        response = self.front.request(method, path, headers=sent, follow_redirects=False)
        self.front.cookies.clear()
        for line in response.headers.get_list("set-cookie"):
            self.set_cookies.setdefault(host, []).append(line)
            parsed = SimpleCookie()
            parsed.load(line)
            for name, morsel in parsed.items():
                if morsel["max-age"] == "0" or not morsel.value.strip('"'):
                    jar.pop(name, None)
                else:
                    jar[name] = morsel.value
        return response

    def verify(self, host, route, **kwargs):
        return self.service(host, "/gate/verify", headers={"X-Vecten-Gate-Route": route}, **kwargs)

    def start(self, host, rd="/docs"):
        started = self.service(host, f"/gate/start?rd={rd}")
        assert started.status_code == 303, started.text
        return started.headers["location"]

    def callback(self, host, url):
        target = urlsplit(url)
        assert (target.scheme, target.netloc, target.path) == (
            "https",
            host,
            "/_vecten/gate/callback",
        )
        return self.service(host, "/gate/callback?" + target.query)

    def sign_in(self, host, email, *, rd="/docs", password=PASSWORD):
        page = self.mcp.get(self.start(host, rd))
        assert page.status_code == 200, page.text
        done = self.mcp.post(
            "/login",
            data=dict(flow=hidden(page, "flow"), csrf=hidden(page, "csrf"), email=email,
                      password=password),
        )  # fmt: skip
        if done.status_code != 200 or "Continue" not in done.text:
            return done
        return self.callback(host, unescape(re.search(r'<a href="([^"]+)"', done.text)[1]))


def hidden(page, name):
    return re.search(rf'name="{name}" value="([^"]+)"', page.text)[1]


@pytest.fixture
def gate():
    now = [1000.0]
    state = StateStore("sqlite://")
    backend = PasswordBackend(state, clock=lambda: now[0])
    for email, name in (("ada@acme.com", "Ada Lovelace"), ("bob@acme.com", "Bob")):
        state.invite(email, name, email.split("@")[0], provisioned=True)
        backend.set_password(backend.setup_link(email), PASSWORD)
    oauth = OAuthService(
        state, SigningKeys(Secrets(), ISSUER), ISSUER, backend, clock=lambda: now[0]
    )
    service = GateService(oauth, ROUTES)
    app = Starlette(routes=auth_routes(oauth, service))
    yield service, Browser(app), now
    state.close()


def person(service, email):
    return service.state.by_email(email)


def test_first_visit_signs_in_on_the_server_and_lands_on_the_path(gate):
    service, browser, _ = gate
    authorize = urlsplit(browser.start(PORTAL, "/docs?page=2&q=a%20b"))
    assert (authorize.scheme, authorize.netloc, authorize.path) == (
        "https",
        "mcp.data.acme.com",
        "/gate/authorize",
    )
    query = parse_qs(authorize.query)
    assert query["host"] == [PORTAL] and query["rd"] == ["/docs?page=2&q=a%20b"]
    verifier = browser.jars[PORTAL]["vecten-gate-state"]
    assert query["challenge"] == [challenge(verifier)]
    assert verifier not in authorize.query
    state_cookie = browser.set_cookies[PORTAL][0].lower()
    assert "path=/_vecten/gate" in state_cookie and "httponly" in state_cookie
    assert "secure" in state_cookie and "domain" not in state_cookie

    landed = browser.sign_in(PORTAL, "ada@acme.com", rd="/docs?page=2&q=a%20b")
    assert landed.status_code == 303 and landed.headers["location"] == "/docs?page=2&q=a%20b"
    cookie = browser.set_cookies[PORTAL][-1]
    assert cookie.startswith("__Host-vecten-gate=")
    attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"httponly", "secure", "path=/", "samesite=lax"} <= attributes
    assert not [a for a in attributes if a.startswith("domain")]
    assert browser.jars.get(DASH) is None

    ada = person(service, "ada@acme.com")
    answer = browser.verify(PORTAL, HOME)
    assert answer.status_code == 200
    assert answer.headers["x-vecten-user"] == "ada@acme.com"
    assert answer.headers["x-vecten-principal"] == ada["id"]
    assert answer.headers["x-vecten-name"] == "Ada Lovelace"
    assert answer.headers["x-vecten-groups"] == "default"
    assert answer.headers["x-vecten-admin"] == "false"


def test_the_session_id_is_stored_only_as_a_hash(gate):
    service, browser, _ = gate
    browser.sign_in(PORTAL, "ada@acme.com")
    sid = browser.jars[PORTAL]["__Host-vecten-gate"]
    with service.state.transaction() as conn:
        rows = conn.execute(select(gate_sessions)).mappings().all()
    assert [row["key"] for row in rows] == [digest(sid)]
    assert sid not in str([dict(row) for row in rows])


def test_a_second_service_in_the_same_browser_needs_no_credentials(gate):
    _, browser, _ = gate
    browser.sign_in(PORTAL, "ada@acme.com")
    passed = browser.mcp.get(browser.start(DASH, "/"), follow_redirects=False)
    assert passed.status_code == 303
    landed = browser.callback(DASH, passed.headers["location"])
    assert landed.status_code == 303 and landed.headers["location"] == "/"
    assert browser.verify(DASH, DASH_HOME).status_code == 200


def test_a_sign_in_through_the_identity_provider_returns_to_the_service(gate):
    service, browser, _ = gate

    class Provider(OIDCBackend):
        def metadata(self):
            return {"authorization_endpoint": "https://idp.example/authorize"}

        def callback(self, state, code):
            pending = self.state.take("oidc_states", digest(state), now=self.clock())
            assert pending and code == "provider-code"
            return Identity("https://idp.example", "ada-sub", "ada@acme.com", "Ada"), pending[
                "flow"
            ]

    # The provider is simulated above, so no HTTP client is ever used.
    service.oauth.backend = Provider(
        service.state, "https://idp.example", "client", ISSUER + "/oidc/callback", client=object()
    )
    page = browser.mcp.get(browser.start(PORTAL, "/"))
    provider = browser.mcp.post(
        "/oidc/start", data={"flow": hidden(page, "flow"), "csrf": hidden(page, "csrf")}
    )
    target = urlsplit(unescape(re.search(r'<a href="([^"]+)"', provider.text)[1]))
    state = parse_qs(target.query)["state"][0]
    returned = browser.mcp.get(
        f"/oidc/callback?state={state}&code=provider-code", follow_redirects=False
    )
    assert returned.status_code == 303, returned.text
    assert browser.callback(PORTAL, returned.headers["location"]).status_code == 303
    assert browser.verify(PORTAL, HOME).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "mcp.data.acme.com", "PORTAL.data.acme.com"])
def test_a_hostname_no_login_route_binds_is_refused_and_issues_no_code(gate, host):
    service, browser, _ = gate
    assert browser.service(host, "/gate/start?rd=/").status_code == 400
    refused = browser.mcp.get(
        "/gate/authorize", params={"host": host, "challenge": "c" * 43, "rd": "/"}
    )
    assert refused.status_code == 400 and refused.headers["content-type"].startswith("text/html")
    assert service.state.scan("flows") == [] and service.state.scan("gate_codes") == []


@pytest.mark.parametrize(
    "rd",
    [
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example",
        "evil.example",
        "",
        "/a b",
        "/a\nb",
    ],
)
def test_a_return_path_that_is_not_a_path_is_refused(gate, rd):
    service, browser, _ = gate
    refused = browser.mcp.get(
        "/gate/authorize", params={"host": PORTAL, "challenge": "c" * 43, "rd": rd}
    )
    assert refused.status_code == 400
    assert service.state.scan("flows") == [] and service.state.scan("gate_codes") == []


def test_a_return_path_through_start_is_refused_unless_it_is_a_path(gate):
    _, browser, _ = gate
    assert browser.service(PORTAL, "/gate/start?rd=https://evil.example/").status_code == 400
    assert browser.service(PORTAL, "/gate/start?rd=//evil.example/").status_code == 400
    assert browser.service(PORTAL, "/gate/start?next=/").status_code == 400
    assert browser.service(PORTAL, "/gate/start").status_code == 303


def _code(browser, host):
    page = browser.mcp.get(browser.start(host, "/"))
    done = browser.mcp.post(
        "/login",
        data=dict(flow=hidden(page, "flow"), csrf=hidden(page, "csrf"), email="ada@acme.com",
                  password=PASSWORD),
    )  # fmt: skip
    return urlsplit(unescape(re.search(r'<a href="([^"]+)"', done.text)[1])).query


def test_a_code_is_refused_a_second_time(gate):
    service, browser, _ = gate
    query = _code(browser, PORTAL)
    assert browser.service(PORTAL, "/gate/callback?" + query).status_code == 303
    browser.jars[PORTAL].pop("__Host-vecten-gate")
    assert browser.service(PORTAL, "/gate/callback?" + query).status_code == 400
    assert "__Host-vecten-gate" not in browser.jars[PORTAL]
    with service.state.transaction() as conn:
        assert len(conn.execute(select(gate_sessions)).all()) == 1


def test_a_code_is_refused_on_another_hostname(gate):
    service, browser, _ = gate
    query = _code(browser, PORTAL)
    stolen = dict(browser.jars[PORTAL])
    assert browser.service(DASH, "/gate/callback?" + query, cookies=stolen).status_code == 400
    # The refusal spent the code.
    assert browser.service(PORTAL, "/gate/callback?" + query).status_code == 400
    with service.state.transaction() as conn:
        assert conn.execute(select(gate_sessions)).all() == []


def test_a_leaked_callback_url_without_the_starting_browsers_cookie_is_refused(gate):
    service, browser, _ = gate
    query = _code(browser, PORTAL)
    assert browser.service(PORTAL, "/gate/callback?" + query, cookies={}).status_code == 400
    other = {"vecten-gate-state": "x" * 43}
    assert browser.service(PORTAL, "/gate/callback?" + query, cookies=other).status_code == 400
    with service.state.transaction() as conn:
        assert conn.execute(select(gate_sessions)).all() == []


def test_a_code_lives_at_most_a_minute(gate):
    _, browser, now = gate
    query = _code(browser, PORTAL)
    now[0] += 61
    assert browser.service(PORTAL, "/gate/callback?" + query).status_code == 400


def test_a_service_principal_gets_no_gate_session(gate):
    service, browser, _ = gate
    robot = service.state.invite("robot@acme.com", "Robot", "robot")
    with service.state.transaction() as conn:
        conn.execute(
            update(principals).where(principals.c.id == robot["id"]).values(kind="service")
        )
    service.state.put(
        "browser_sessions", digest("robot-browser"), {"principal_id": robot["id"]}, expires_at=1e9
    )
    browser.start(PORTAL, "/")
    verifier = browser.jars[PORTAL]["vecten-gate-state"]
    browser.mcp.cookies.set("vecten_browser", "robot-browser", domain="mcp.data.acme.com")
    refused = browser.mcp.get(
        "/gate/authorize",
        params={"host": PORTAL, "challenge": challenge(verifier), "rd": "/"},
        follow_redirects=False,
    )
    assert refused.status_code == 400
    assert service.state.scan("gate_codes") == []
    service.state.open_gate_session(
        digest("robot-gate"), robot["id"], PORTAL, now=1000, expires_at=10**9
    )
    planted = browser.verify(PORTAL, HOME, cookies={"__Host-vecten-gate": "robot-gate"})
    assert planted.status_code == 401


def test_a_disabled_person_is_sent_to_sign_in_and_cannot(gate):
    service, browser, _ = gate
    browser.sign_in(PORTAL, "bob@acme.com")
    session = dict(browser.jars[PORTAL])
    assert browser.verify(PORTAL, HOME).status_code == 200
    bob = person(service, "bob@acme.com")
    service.state.update_principal(bob["id"], enabled=False)
    # Disabled in state alone, before any session is ended: the next request is refused.
    assert browser.verify(PORTAL, HOME, cookies=session).status_code == 401
    service.oauth.disable_principal(bob["id"])
    with service.state.transaction() as conn:
        assert conn.execute(select(gate_sessions)).all() == []
    again = Browser(browser.mcp.app).sign_in(PORTAL, "bob@acme.com")
    assert again.status_code == 400 and "Sign-in failed" in again.text


def test_verify_answers_401_without_a_session_for_that_hostname(gate):
    _, browser, now = gate
    assert browser.verify(PORTAL, HOME).status_code == 401
    browser.sign_in(PORTAL, "ada@acme.com")
    session = browser.jars[PORTAL]
    refused = browser.verify(DASH, DASH_HOME, cookies=dict(session))
    assert refused.status_code == 401
    assert not [h for h in refused.headers if h.lower().startswith("x-vecten")]
    assert browser.verify(PORTAL, HOME, cookies={"__Host-vecten-gate": "guess"}).status_code == 401
    now[0] += 604800
    assert browser.verify(PORTAL, HOME).status_code == 401


def test_verify_answers_403_outside_the_routes_groups_and_follows_live_membership(gate):
    service, browser, _ = gate
    browser.sign_in(PORTAL, "bob@acme.com")
    refused = browser.verify(PORTAL, BOOKS)
    assert refused.status_code == 403
    assert not [h for h in refused.headers if h.lower().startswith("x-vecten")]
    bob = person(service, "bob@acme.com")
    with service.state.transaction() as conn:
        conn.execute(groups.insert().values(name="finance", warehouse_role="finance"))
        conn.execute(memberships.insert().values(principal_id=bob["id"], group_name="finance"))
    admitted = browser.verify(PORTAL, BOOKS)
    assert admitted.status_code == 200
    assert admitted.headers["x-vecten-groups"] == "default,finance"
    with service.state.transaction() as conn:
        conn.execute(memberships.delete().where(memberships.c.group_name == "finance"))
    assert browser.verify(PORTAL, BOOKS).status_code == 403


def test_verify_refuses_a_route_the_server_does_not_bind_on_that_hostname(gate):
    _, browser, _ = gate
    browser.sign_in(PORTAL, "ada@acme.com")
    assert browser.verify(PORTAL, DASH_HOME).status_code == 403
    assert browser.verify(PORTAL, "acme-portal.web.gone").status_code == 403


def test_verify_is_not_limited_per_address(gate):
    _, browser, _ = gate
    browser.sign_in(PORTAL, "ada@acme.com")
    assert {browser.verify(PORTAL, HOME).status_code for _ in range(300)} == {200}


def test_identity_headers_carry_a_name_outside_ascii_percent_encoded(gate):
    service, browser, _ = gate
    browser.sign_in(PORTAL, "ada@acme.com")
    ada = person(service, "ada@acme.com")
    service.state.update_principal(ada["id"], name="Zoë 李 100%\r\nX-Evil: 1", admin=True)
    answer = browser.verify(PORTAL, HOME)
    assert answer.headers["x-vecten-name"] == "Zo%C3%AB %E6%9D%8E 100%25%0D%0AX-Evil: 1"
    assert answer.headers["x-vecten-admin"] == "true"
    assert "x-evil" not in answer.headers


def test_logout_ends_the_session_and_clears_the_cookie(gate):
    _, browser, _ = gate
    browser.sign_in(PORTAL, "ada@acme.com")
    session = dict(browser.jars[PORTAL])
    out = browser.service(PORTAL, "/gate/logout")
    assert out.status_code == 200
    cleared = browser.set_cookies[PORTAL][-1].lower()
    assert cleared.startswith("__host-vecten-gate=") and "max-age=0" in cleared
    assert "secure" in cleared and "path=/" in cleared
    assert browser.verify(PORTAL, HOME, cookies=session).status_code == 401


def test_setting_a_password_ends_the_persons_gate_sessions(gate):
    service, browser, _ = gate
    browser.sign_in(PORTAL, "ada@acme.com")
    other = Browser(browser.mcp.app)
    other.sign_in(PORTAL, "bob@acme.com")
    page = browser.mcp.get("/set-password")
    reset = browser.mcp.post(
        "/set-password",
        data={
            "csrf": hidden(page, "csrf"),
            "token": service.oauth.backend.setup_link("ada@acme.com"),
            "password": "another-long-password",
        },
    )
    assert reset.status_code == 200
    assert browser.verify(PORTAL, HOME).status_code == 401
    assert other.verify(PORTAL, HOME).status_code == 200


def test_a_password_set_ends_gate_sessions_within_its_own_transaction(gate):
    service, browser, _ = gate
    browser.sign_in(PORTAL, "ada@acme.com")
    backend = service.oauth.backend
    backend.set_password(backend.setup_link("ada@acme.com"), "another-long-password")
    assert browser.verify(PORTAL, HOME).status_code == 401


def config(services):
    return VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "extensions": ["acme-portal", "acme-hook"],
            "services": services,
        }
    )


SERVICES = {
    "acme-portal": {
        "web": {
            "hostname": "portal",
            "routes": {
                "home": {"access": "login", "groups": ["default"]},
                "hook": {"access": "public"},
            },
        },
        "apex": {"hostname": "@", "routes": {"home": {"access": "login", "groups": ["sales"]}}},
    },
    "acme-hook": {"bot": {"hostname": "bot", "routes": {"hook": {"access": "public"}}}},
}


def test_the_server_reads_its_login_routes_from_vecten_toml(gate):
    service, _, _ = gate
    built = GateService.from_config(service.oauth, config(SERVICES))
    assert built.routes == {
        "acme-portal.web.home": GateRoute("portal.data.acme.com", ("default",)),
        "acme-portal.apex.home": GateRoute("data.acme.com", ("sales",)),
    }
    assert built.origin("portal.data.acme.com") == "https://portal.data.acme.com"


def test_on_the_laptop_a_service_hostname_keeps_the_published_port(gate):
    service, _, _ = gate
    oauth = OAuthService(
        service.state, service.oauth.keys, "https://mcp.localhost:8443", service.oauth.backend
    )
    built = GateService.from_config(oauth, config(SERVICES))
    assert built.routes["acme-portal.web.home"].host == "portal.localhost"
    assert built.origin("portal.localhost") == "https://portal.localhost:8443"


def test_the_server_answers_the_gate_when_its_runtime_has_one(gate):
    from vecten_mcp.app import create_app
    from vecten_mcp.extensions import McpExtensions
    from vecten_mcp.runtime import Runtime

    service, _, _ = gate
    runtime = Runtime(
        service.state, service.oauth, McpExtensions({}, {}, {}, {}), None, None, gate=service
    )
    with TestClient(create_app(runtime=runtime), base_url=ISSUER) as http:
        refused = http.get("/gate/verify", headers={"X-Vecten-Gate-Host": PORTAL})
        assert refused.status_code == 401
        assert http.get("/gate/start?rd=/", headers={"X-Vecten-Gate-Host": PORTAL},
                        follow_redirects=False).status_code == 303  # fmt: skip
