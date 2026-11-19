"""`/apps`: a signed-in person sees the apps one of their current groups is admitted to."""

import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.apps import AppScopes, BoundApp
from vecten_mcp.panel.people import APPS_CLIENT
from vecten_shared.identity_setup import ACCESS_VERSION

spec = importlib.util.spec_from_file_location(
    "apps_page_session_fixture", Path(__file__).with_name("test_panel_session.py")
)
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)
builtins = session.builtins

SALES = BoundApp(
    "acme-sales",
    "overview",
    "Sales overview",
    "container",
    ("orders",),
    ("sales",),
    hostname="sales",
)
FINANCE = BoundApp(
    "acme-finance",
    "ledger",
    "Finance ledger",
    "container",
    ("revenue",),
    ("finance",),
    hostname="ledger",
)
WAREHOUSE = BoundApp(
    "acme-sales", "warehouse", "Warehouse dashboard", "snowflake", ("orders",), ("sales",)
)
GROUPLESS = BoundApp(
    "acme-sales",
    "regions",
    "Regions",
    "container",
    ("orders",),
    ("sales", "marketing"),
    hostname="regions",
)


class Adapter:
    def create_group(self, role):
        pass

    def membership(self, role, name, grant=True):
        pass

    def dataset_grant(self, role, source, grant=True):
        pass

    def schema_grant(self, role, schema, grant=True):
        pass

    def access_version(self):
        return ACCESS_VERSION

    def read_grants(self, roles):
        return set()

    def granted_roles(self, role):
        return set()


@pytest.fixture
def page(builtins):
    runtime = builtins.runtime
    # marts_finance is the declared area finance's schema.
    runtime.access = AccessService(runtime.state, Adapter(), areas={"finance"})
    access = runtime.access
    access.datasets = {"orders": "marts.orders", "revenue": "marts_finance.revenue"}
    for group in ("sales", "finance", "marketing"):
        access.create_group(group)
    access.grant("sales", "dataset", "orders")
    access.grant("finance", "dataset", "revenue")
    runtime.apps = AppScopes(
        runtime.state,
        {app.name: app for app in (SALES, FINANCE, WAREHOUSE, GROUPLESS)},
        kind="postgres",
    )
    access.apps = runtime.apps
    access.admit()
    analyst = session.person(builtins, "ana@example.com", admin=False)
    access.membership("sales", analyst["id"])
    access.membership("marketing", analyst["id"])
    builtins.analyst = analyst
    return builtins


def sign_in(http, email="ana@example.com"):
    return http.get(session.panel_authorized(http, email, base="/apps"), follow_redirects=False)


def test_a_person_not_signed_in_is_sent_to_sign_in(page):
    response = page.http.get("/apps", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/apps/login"


def test_sign_in_uses_its_own_client_with_pkce_and_a_cookie_of_its_own(page):
    start = page.http.get("/apps/login", follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == [APPS_CLIENT]
    assert query["redirect_uri"] == ["https://mcp.example/apps/callback"]
    assert query["code_challenge_method"] == ["S256"]
    callback = sign_in(page.http)
    assert callback.status_code == 303 and callback.headers["location"] == "/apps"
    cookie = callback.headers["set-cookie"].lower()
    assert cookie.startswith("__host-vecten_apps=") and "path=/;" in cookie + ";"
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_a_person_in_sales_sees_the_sales_app_and_nothing_about_finance(page):
    sign_in(page.http)
    response = page.http.get("/apps")
    assert response.status_code == 200, response.text
    assert '<a href="https://sales.example/">Sales overview</a>' in response.text
    assert "Finance" not in response.text and "ledger" not in response.text
    assert "acme-finance" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_an_app_whose_admitting_group_lacks_the_person_or_a_link_is_not_listed(page):
    sign_in(page.http)
    text = page.http.get("/apps").text
    # Admitted through sales, which holds the person: listed once.
    assert text.count("Regions") == 1
    # A snowflake app has no link until it is published.
    assert "Warehouse dashboard" not in text


def test_a_group_bound_but_not_admitted_shows_nothing(page):
    access = page.runtime.access
    access.membership("sales", page.analyst["id"], False)
    sign_in(page.http)
    text = page.http.get("/apps").text
    # marketing is bound to regions but holds no grant on orders, so it admits no one.
    assert "Regions" not in text and "Sales overview" not in text
    assert "No analytics app is open to your groups" in text


def test_the_list_follows_the_persons_current_groups(page):
    sign_in(page.http)
    assert "Sales overview" in page.http.get("/apps").text
    page.runtime.access.membership("sales", page.analyst["id"], False)
    assert "Sales overview" not in page.http.get("/apps").text


def test_an_admin_panel_session_is_not_an_apps_session(page):
    session.person(page, "admin@example.com")
    assert session.panel_login(page.http).status_code == 303
    response = page.http.get("/apps", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/apps/login"


def test_signing_out_ends_the_session(page):
    sign_in(page.http)
    csrf = session.hidden(page.http.get("/apps"), "csrf")
    assert page.http.post("/apps/logout", data={"csrf": csrf}).status_code == 200
    response = page.http.get("/apps", follow_redirects=False)
    assert response.status_code == 303


def test_signing_out_without_the_form_token_is_refused(page):
    sign_in(page.http)
    assert page.http.post("/apps/logout", data={"csrf": "wrong"}).status_code == 403
    assert page.http.get("/apps").status_code == 200


def test_without_bound_apps_the_page_lists_none(builtins):
    session.person(builtins, "ana@example.com", admin=False)
    sign_in(builtins.http)
    assert "No analytics app is open to your groups" in builtins.http.get("/apps").text


def cookie(http, name):
    return next(c for c in http.cookies.jar if c.name == name)


def test_the_apps_cookie_is_secure(page):
    sign_in(page.http)
    assert cookie(page.http, "__Host-vecten_apps").secure


def test_a_panel_cookie_replayed_as_the_apps_cookie_is_not_a_session(page):
    session.person(page, "admin@example.com")
    assert session.panel_login(page.http).status_code == 303
    value = cookie(page.http, "__Host-vecten_panel").value
    page.http.cookies.clear()
    page.http.cookies.set("__Host-vecten_apps", value, domain="mcp.example")
    response = page.http.get("/apps", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/apps/login"


def test_an_apps_cookie_replayed_as_the_panel_cookie_is_not_a_session(page):
    session.person(page, "admin@example.com")
    sign_in(page.http, "admin@example.com")
    value = cookie(page.http, "__Host-vecten_apps").value
    page.http.cookies.clear()
    page.http.cookies.set("__Host-vecten_panel", value, domain="mcp.example")
    response = page.http.get("/admin", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"


def test_an_apps_session_does_not_open_the_panel(page):
    session.person(page, "admin@example.com")
    sign_in(page.http, "admin@example.com")
    response = page.http.get("/admin", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"


def test_app_text_on_the_page_is_escaped(page):
    page.runtime.apps.apps["overview"] = BoundApp(
        "acme-sales",
        "overview",
        "<script>alert(1)</script>",
        "container",
        ("orders",),
        ("sales",),
        description="<script>alert(2)</script>",
        hostname="sales",
    )
    sign_in(page.http)
    body = page.http.get("/apps").text
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)" in body and "&lt;script&gt;alert(2)" in body


SURFACES = {"/admin": "vecten_panel", "/apps": "vecten_apps"}


def stranger(page):
    """Another browser on the same server."""
    from vecten_mcp.app import create_app

    return session.fixture.TestClient(
        create_app(runtime=page.runtime), base_url="https://mcp.example"
    )


@pytest.mark.parametrize("base", SURFACES)
def test_a_planted_login_cookie_does_not_finish_the_planters_sign_in(page, base):
    # A sibling hostname can set a cookie of any name but a __Host- one for the whole domain.
    session.person(page, "mallory@example.com")
    callback = session.panel_authorized(page.http, "mallory@example.com", base=base)
    login = SURFACES[base] + "_login"
    nonce = next(c.value for c in page.http.cookies.jar if c.name.endswith(login))
    with stranger(page) as victim:
        planted = {"Cookie": f"{login}={nonce}"}
        assert victim.get(callback, headers=planted, follow_redirects=False).status_code == 400
        assert victim.get(base, follow_redirects=False).status_code == 303
    # The planter's own browser still holds the flow.
    assert page.http.get(callback, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("base", SURFACES)
def test_a_planted_session_cookie_is_not_a_session(page, base):
    session.person(page, "mallory@example.com")
    callback = session.panel_authorized(page.http, "mallory@example.com", base=base)
    assert page.http.get(callback, follow_redirects=False).status_code == 303
    name = SURFACES[base]
    token = next(c.value for c in page.http.cookies.jar if c.name.endswith(name))
    with stranger(page) as victim:
        response = victim.get(base, headers={"Cookie": f"{name}={token}"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == base + "/login"


@pytest.mark.parametrize("base", SURFACES)
def test_a_loopback_tunnel_signs_in_with_path_scoped_cookies(page, base):
    from vecten_mcp.app import create_app

    session.person(page, "mallory@example.com")
    with session.fixture.TestClient(
        create_app(runtime=page.runtime), base_url="http://127.0.0.1:8001"
    ) as tunnel:
        start = tunnel.get(base + "/login", follow_redirects=False)
        callback = tunnel.get(
            session.panel_authorized(tunnel, "mallory@example.com", base=base),
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert tunnel.get(base).status_code == 200
    name = SURFACES[base]
    for response, cookie in ((start, name + "_login"), (callback, name)):
        line = next(
            line.lower()
            for line in response.headers.get_list("set-cookie")
            if line.startswith(cookie + "=")
        )
        assert f"path={base}" in line and "secure" not in line


@pytest.mark.parametrize("base", SURFACES)
def test_signing_in_and_out_clear_the_host_only_cookies(page, base):
    # A browser keeps a __Host- cookie unless the clearing header is Secure with Path=/.
    session.person(page, "mallory@example.com")
    name = "__host-" + SURFACES[base]
    callback = page.http.get(
        session.panel_authorized(page.http, "mallory@example.com", base=base),
        follow_redirects=False,
    )
    csrf = session.hidden(page.http.get(base), "csrf")
    out = page.http.post(base + "/logout", data={"csrf": csrf}, follow_redirects=False)
    for response, cleared in ((callback, name + "_login"), (out, name)):
        line = next(
            line.lower()
            for line in response.headers.get_list("set-cookie")
            if line.lower().startswith(cleared + "=")
        )
        assert "max-age=0" in line and "path=/;" in line + ";" and "secure" in line
