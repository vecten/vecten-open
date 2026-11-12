"""`/apps`: a signed-in person sees the apps one of their current groups is admitted to."""

import importlib.util
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.apps import AppScopes, BoundApp
from vecten_mcp.panel.people import APPS_CLIENT

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


@pytest.fixture
def page(builtins):
    runtime = builtins.runtime
    runtime.access = AccessService(runtime.state, Adapter())
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
    start = http.get("/apps/login", follow_redirects=False)
    assert start.status_code == 303, start.text
    form = http.get(start.headers["location"])
    consent = http.post(
        "/login",
        data={
            "flow": session.hidden(form, "flow"),
            "csrf": session.hidden(form, "csrf"),
            "email": email,
            "password": session.PASSWORD,
        },
    )
    redirect = http.post(
        "/consent",
        data={
            "flow": session.hidden(consent, "flow"),
            "csrf": session.hidden(consent, "csrf"),
            "decision": "approve",
        },
    )
    target = urlparse(unescape(re.search(r'<a href="([^"]+)"', redirect.text)[1]))
    return http.get(target.path + "?" + target.query, follow_redirects=False)


def test_a_person_not_signed_in_is_sent_to_sign_in(page):
    response = page.http.get("/apps", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/apps/login"


def test_sign_in_uses_its_own_client_with_pkce_and_a_cookie_for_apps_only(page):
    start = page.http.get("/apps/login", follow_redirects=False)
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["client_id"] == [APPS_CLIENT]
    assert query["redirect_uri"] == ["https://mcp.example/apps/callback"]
    assert query["code_challenge_method"] == ["S256"]
    callback = sign_in(page.http)
    assert callback.status_code == 303 and callback.headers["location"] == "/apps"
    cookie = callback.headers["set-cookie"].lower()
    assert "path=/apps" in cookie and "httponly" in cookie and "samesite=strict" in cookie


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
