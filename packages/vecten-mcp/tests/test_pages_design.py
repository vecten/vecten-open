"""The MCP's browser pages share the Vecten design system's stylesheet and fonts, served by the
MCP itself, under a CSP that admits only same-origin styles and fonts."""

import re

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from vecten_mcp.assets import ASSETS, LOGO, asset_routes
from vecten_mcp.auth.pages import consent_page, login_page, page, redirect_page
from vecten_mcp.panel.html import layout

SIGN_IN_CSP = (
    "default-src 'none'; style-src 'self'; font-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)


@pytest.fixture
def client():
    return TestClient(Starlette(routes=asset_routes()))


def test_the_stylesheet_and_fonts_are_served_from_the_mcp(client):
    css = client.get("/assets/vecten.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert css.headers["x-content-type-options"] == "nosniff"
    for name in ("Geist-Variable.ttf", "GeistMono-Variable.ttf"):
        font = client.get(f"/assets/fonts/{name}")
        assert font.status_code == 200 and font.headers["content-type"] == "font/ttf"
        assert font.content[:4] == b"\x00\x01\x00\x00"
    assert "SIL Open Font License" in client.get("/assets/fonts/OFL.txt").text


@pytest.mark.parametrize("path", ["/assets/nothing.css", "/assets/../app.py", "/assets/fonts"])
def test_only_the_listed_assets_are_served(client, path):
    assert client.get(path).status_code == 404


def test_every_font_the_stylesheet_names_is_served(client):
    css = client.get("/assets/vecten.css").text
    named = re.findall(r'url\("/assets/([^"]+)"\)', css)
    assert named and set(named) <= set(ASSETS)


def test_the_stylesheet_has_a_light_and_a_dark_theme(client):
    css = client.get("/assets/vecten.css").text
    assert "@media (prefers-color-scheme: dark)" in css
    assert "--midnight-900: #10233c" in css and "--gold-400: #f3b838" in css
    # The CSP admits no other origin, so nothing may load from a CDN or Google Fonts.
    assert "http" not in css


def _no_inline_style_or_script(html):
    assert "<script" not in html and " style=" not in html and "<style" not in html


@pytest.mark.parametrize(
    "response",
    [
        login_page("f", "c"),
        login_page("f", "c", oidc=True),
        login_page("f", "c", error="That did not work."),
        consent_page("f", "c", "Claude"),
        redirect_page("https://claude.ai/callback"),
        page("Password set", "<p>Return to your client to sign in.</p>"),
    ],
)
def test_sign_in_pages_take_the_shared_stylesheet_under_a_same_origin_csp(response):
    html = response.body.decode()
    assert response.headers["content-security-policy"] == SIGN_IN_CSP
    assert '<link rel="stylesheet" href="/assets/vecten.css">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert LOGO in html
    _no_inline_style_or_script(html)


def test_consent_offers_approve_as_the_primary_action_and_deny_beside_it():
    html = consent_page("f", "c", "Claude").body.decode()
    assert '<button name="decision" value="approve">Approve</button>' in html
    assert '<button name="decision" value="deny" class="secondary">Deny</button>' in html


def test_the_admin_panel_takes_the_shared_stylesheet_and_fonts():
    response = layout("Home", "<p>hi</p>", csrf="c")
    html = response.body.decode()
    csp = response.headers["content-security-policy"]
    assert "style-src 'self'" in csp and "font-src 'self'" in csp
    assert "default-src 'none'" in csp
    assert '<link rel="stylesheet" href="/assets/vecten.css">' in html
    assert LOGO in html
    _no_inline_style_or_script(html)
