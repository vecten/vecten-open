import importlib.util
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from vecten_mcp.identity import Identity
from vecten_mcp.panel.session import PANEL_CLIENT

spec = importlib.util.spec_from_file_location(
    "panel_builtin_fixture", Path(__file__).with_name("test_builtin_tools.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
builtins = fixture.builtins
PASSWORD = "long-fixture-password"


def person(builtins, email="admin@example.com", *, admin=True):
    state, backend = builtins.runtime.state, builtins.runtime.oauth.backend
    created = state.create_principal(
        Identity("password", email, email, email.split("@")[0].title()),
        email.split("@")[0].replace(".", "_"),
        admin=admin,
        provisioned=True,
    )
    backend.set_password(backend.setup_link(email), PASSWORD)
    return created


def hidden(page, name):
    return unescape(re.search(rf'name="{name}" value="([^"]+)"', page.text)[1])


def panel_login(http, email="admin@example.com", password=PASSWORD):
    """Drive the panel's first-party authorization code flow; return the callback response."""
    start = http.get("/admin/login", follow_redirects=False)
    assert start.status_code == 303, start.text
    page = http.get(start.headers["location"])
    assert page.status_code == 200, page.text
    consent = http.post(
        "/login",
        data={
            "flow": hidden(page, "flow"),
            "csrf": hidden(page, "csrf"),
            "email": email,
            "password": password,
        },
    )
    assert consent.status_code == 200, consent.text
    redirect = http.post(
        "/consent",
        data={
            "flow": hidden(consent, "flow"),
            "csrf": hidden(consent, "csrf"),
            "decision": "approve",
        },
    )
    target = unescape(re.search(r'<a href="([^"]+)"', redirect.text)[1])
    parsed = urlparse(target)
    return http.get(parsed.path + "?" + parsed.query, follow_redirects=False)


def csrf_of(http):
    return hidden(http.get("/admin"), "csrf")


@pytest.fixture
def signed_in(builtins):
    admin = person(builtins)
    callback = panel_login(builtins.http)
    assert callback.status_code == 303 and callback.headers["location"] == "/admin"
    return admin


def test_unauthenticated_panel_redirects_to_login(builtins):
    response = builtins.http.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_login_redirect_uses_the_first_party_client_with_pkce(builtins):
    start = builtins.http.get("/admin/login", follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == [PANEL_CLIENT]
    assert query["redirect_uri"] == ["https://mcp.example/admin/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [builtins.runtime.oauth.resource]
    cookie = start.headers["set-cookie"].lower()
    assert "samesite=strict" in cookie and "httponly" in cookie and "secure" in cookie


def test_admin_signs_in_and_sees_the_panel(builtins, signed_in):
    cookie = [c for c in builtins.http.cookies.jar if c.name == "vecten_panel"]
    assert cookie and cookie[0].secure
    home = builtins.http.get("/admin")
    assert home.status_code == 200
    assert "Vecten admin" in home.text
    assert "default-src 'none'" in home.headers["content-security-policy"]
    assert home.headers["cache-control"] == "no-store"


def test_session_cookie_is_strict_and_http_only(builtins):
    person(builtins)
    callback = panel_login(builtins.http)
    header = callback.headers["set-cookie"].lower()
    assert "vecten_panel=" in header
    assert "samesite=strict" in header and "httponly" in header and "secure" in header


def test_non_admin_is_refused_after_signing_in(builtins):
    person(builtins, "jane@example.com", admin=False)
    person(builtins, "admin@example.com")
    callback = panel_login(builtins.http, "jane@example.com")
    assert callback.status_code == 403
    assert "vecten_panel" not in {c.name for c in builtins.http.cookies.jar}
    assert builtins.http.get("/admin", follow_redirects=False).status_code == 303


def test_panel_session_outlives_the_access_token_up_to_eight_hours(builtins, signed_in):
    import time

    oauth = builtins.runtime.oauth
    start = time.time()
    oauth.clock = lambda: start + 2 * 3600
    assert builtins.http.get("/admin", follow_redirects=False).status_code == 200
    oauth.clock = lambda: start + 9 * 3600
    assert builtins.http.get("/admin", follow_redirects=False).status_code == 303


def test_cleared_flag_refuses_the_next_request(builtins, signed_in):
    builtins.runtime.state.update_principal(signed_in["id"], admin=False)
    assert builtins.http.get("/admin").status_code == 403


def test_disabled_admin_is_refused_on_the_next_request(builtins, signed_in):
    person(builtins, "second@example.com")
    builtins.runtime.oauth.disable_principal(signed_in["id"])
    response = builtins.http.get("/admin", follow_redirects=False)
    assert response.status_code == 303


def test_post_without_csrf_changes_nothing(builtins, signed_in):
    builtins.runtime.state.put("gaps", "g1", {"status": "open"})
    response = builtins.http.post("/admin/gaps/g1", data={"status": "triaged"})
    assert response.status_code == 403
    response = builtins.http.post("/admin/gaps/g1", data={"status": "triaged", "csrf": "forged"})
    assert response.status_code == 403
    assert builtins.runtime.state.get("gaps", "g1")["status"] == "open"


def test_csrf_of_another_session_is_refused(builtins, signed_in):
    stolen = csrf_of(builtins.http)
    builtins.http.cookies.clear()
    panel_login(builtins.http)
    builtins.runtime.state.put("gaps", "g1", {"status": "open"})
    response = builtins.http.post("/admin/gaps/g1", data={"status": "triaged", "csrf": stolen})
    assert response.status_code == 403
    assert builtins.runtime.state.get("gaps", "g1")["status"] == "open"


def test_logout_ends_the_panel_and_oauth_sessions(builtins, signed_in):
    csrf = csrf_of(builtins.http)
    sessions = len(builtins.runtime.state.scan("sessions"))
    response = builtins.http.post("/admin/logout", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 200
    assert len(builtins.runtime.state.scan("sessions")) == sessions - 1
    assert builtins.http.get("/admin", follow_redirects=False).status_code == 303


def test_callback_with_a_wrong_state_is_refused(builtins):
    person(builtins)
    builtins.http.get("/admin/login", follow_redirects=False)
    response = builtins.http.get("/admin/callback?code=x&state=forged", follow_redirects=False)
    assert response.status_code == 400


def test_loopback_origin_completes_login_through_a_tunnel(builtins):
    from vecten_mcp.app import create_app

    person(builtins)
    with fixture.TestClient(
        create_app(runtime=builtins.runtime), base_url="http://127.0.0.1:8001"
    ) as tunnel:
        start = tunnel.get("/admin/login", follow_redirects=False)
        query = parse_qs(urlparse(start.headers["location"]).query)
        assert query["redirect_uri"] == ["http://127.0.0.1:8001/admin/callback"]
        assert panel_login(tunnel).status_code == 303
        assert tunnel.get("/admin").status_code == 200
    client = builtins.runtime.state.get("clients", PANEL_CLIENT)
    assert "https://mcp.example/admin/callback" in client["redirect_uris"]


def test_login_cookies_stay_secure_except_on_a_loopback_tunnel(builtins):
    from vecten_mcp.app import create_app

    start = builtins.http.get("/admin/login", follow_redirects=False)
    page = builtins.http.get(start.headers["location"])
    assert "secure" in page.headers["set-cookie"].lower()
    with fixture.TestClient(
        create_app(runtime=builtins.runtime), base_url="http://localhost:9000"
    ) as tunnel:
        start = tunnel.get("/admin/login", follow_redirects=False)
        page = tunnel.get(start.headers["location"])
        assert "secure" not in page.headers["set-cookie"].lower()
    spoofed = builtins.http.get(start.headers["location"], headers={"host": "127.0.0.1"})
    assert "secure" in spoofed.headers["set-cookie"].lower()


class StubOIDC:
    """An OIDC backend whose provider is simulated; begin/flow lookup stay the real code."""

    @staticmethod
    def install(builtins):
        from vecten_mcp.auth.identity import OIDCBackend

        class Backend(OIDCBackend):
            def metadata(self):
                return {"authorization_endpoint": "https://idp.example/authorize"}

            def callback(self, state, code):
                pending = self.state.take("oidc_states", session_digest(state), now=self.clock())
                if not pending or code != "provider-code":
                    raise ValueError("OIDC state is invalid or expired")
                identity = Identity(
                    "https://idp.example", "admin-subject", "admin@example.com", "Admin"
                )
                return identity, pending["flow"]

        state = builtins.runtime.state
        state.invite("admin@example.com", "Admin", "admin", admin=True, provisioned=True)
        builtins.runtime.oauth.backend = Backend(
            state, "https://idp.example", "panel-client", "https://mcp.example/oidc/callback"
        )


def session_digest(value):
    from vecten_mcp.auth.identity import digest

    return digest(value)


def oidc_tunnel_login(builtins, public, tunnel):
    start = tunnel.get("/admin/login", follow_redirects=False)
    page = tunnel.get(start.headers["location"])
    assert 'action="/oidc/start"' in page.text
    provider = tunnel.post(
        "/oidc/start", data={"flow": hidden(page, "flow"), "csrf": hidden(page, "csrf")}
    )
    assert provider.status_code == 200, provider.text
    target = urlparse(unescape(re.search(r'<a href="([^"]+)"', provider.text)[1]))
    assert target.netloc == "idp.example"
    state = parse_qs(target.query)["state"][0]
    # The provider returns to the public issuer, whose cookie jar holds nothing for this flow.
    returned = public.get(
        f"/oidc/callback?state={state}&code=provider-code", follow_redirects=False
    )
    return returned, state


def test_oidc_sign_in_through_the_tunnel_returns_to_the_loopback_origin(builtins):
    from vecten_mcp.app import create_app

    StubOIDC.install(builtins)
    public = builtins.http
    with fixture.TestClient(
        create_app(runtime=builtins.runtime), base_url="http://127.0.0.1:8001"
    ) as tunnel:
        returned, state = oidc_tunnel_login(builtins, public, tunnel)
        assert returned.status_code == 303, returned.text
        handoff = urlparse(returned.headers["location"])
        assert (handoff.scheme, handoff.netloc, handoff.path) == (
            "http",
            "127.0.0.1:8001",
            "/oidc/callback",
        )
        assert parse_qs(handoff.query) == {"state": [state], "code": ["provider-code"]}
        consent = tunnel.get(handoff.path + "?" + handoff.query)
        assert consent.status_code == 200, consent.text
        redirect = tunnel.post(
            "/consent",
            data={
                "flow": hidden(consent, "flow"),
                "csrf": hidden(consent, "csrf"),
                "decision": "approve",
            },
        )
        target = urlparse(unescape(re.search(r'<a href="([^"]+)"', redirect.text)[1]))
        assert target.netloc == "127.0.0.1:8001" and target.path == "/admin/callback"
        callback = tunnel.get(target.path + "?" + target.query, follow_redirects=False)
        assert callback.status_code == 303 and callback.headers["location"] == "/admin"
        assert tunnel.get("/admin").status_code == 200
    assert not list(public.cookies.jar)


def test_public_oidc_sign_in_still_requires_the_browser_cookie(builtins):
    StubOIDC.install(builtins)
    start = builtins.http.get("/admin/login", follow_redirects=False)
    page = builtins.http.get(start.headers["location"])
    provider = builtins.http.post(
        "/oidc/start", data={"flow": hidden(page, "flow"), "csrf": hidden(page, "csrf")}
    )
    target = urlparse(unescape(re.search(r'<a href="([^"]+)"', provider.text)[1]))
    state = parse_qs(target.query)["state"][0]
    from vecten_mcp.app import create_app

    with fixture.TestClient(
        create_app(runtime=builtins.runtime), base_url="https://mcp.example"
    ) as stranger:
        refused = stranger.get(
            f"/oidc/callback?state={state}&code=provider-code", follow_redirects=False
        )
    assert refused.status_code == 400
    assert builtins.runtime.state.get("oidc_states", session_digest(state)) is None


def test_foreign_host_falls_back_to_the_issuer_callback(builtins):
    start = builtins.http.get(
        "/admin/login", headers={"host": "evil.example"}, follow_redirects=False
    )
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["redirect_uri"] == ["https://mcp.example/admin/callback"]
