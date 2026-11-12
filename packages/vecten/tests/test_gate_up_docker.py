"""The laptop stack's gate: a person signs in with the password backend and reaches a login
route of an in-repo extension's service, which sees who they are and nothing forged."""

import base64
import hashlib
import json
import re
import secrets
import ssl
import subprocess
import sys
import uuid
from html import unescape
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from vecten.stack.compose import STACK_DIR, compose_project_name
from vecten_shared.config import load_config
from vecten_shared.provider.local import LocalProvider
from vecten_shared.versions import VERSIONS

from .docker_util import remove_stack_images
from .test_up_e2e import REPO, docker_ok, drop_volumes, vecten

pytestmark = pytest.mark.docker

ADMIN = "ada@acme.com"
PASSWORD = "gate-fixture-password"
PYPROJECT = """\
[project]
name = "acme-gate"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = []
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"
[project.entry-points."vecten.services"]
web = "acme_gate:web"
"""
PACKAGE = """\
web = {
    "command": ["python", "-m", "acme_gate.web"],
    "port": 8000,
    "health": {"path": "/healthz"},
    "routes": {
        "home": {"path": "/", "access": "login", "groups": ["default"]},
        "books": {"path": "/books", "access": "login", "groups": ["finance"]},
        "hook": {"path": "/hook", "access": "public"},
    },
}
"""
# Answers every request with the headers it received, as JSON.
WEB = """\
import http.server, json


class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET


http.server.ThreadingHTTPServer(("", 8000), Echo).serve_forever()
"""
WEB_HOST = "web.localhost:8443"
MCP_HOST = "mcp.localhost:8443"
FORGED = {"X-Vecten-User": "mallory@evil.example", "X-Vecten-Groups": "finance"}


class Browser:
    """Cookies per host, kept by hand: the requests all go to 127.0.0.1 with a Host header."""

    def __init__(self, client):
        self.client = client
        self.jars: dict[str, dict[str, str]] = {}

    def go(self, method, url, *, headers=None, data=None, json=None):
        target = urlsplit(url)
        host = target.netloc
        jar = self.jars.setdefault(host, {})
        sent = {"Host": host, **(headers or {})}
        if jar:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        path = target.path + (f"?{target.query}" if target.query else "")
        response = self.client.request(
            method,
            f"https://127.0.0.1:8443{path}",
            headers=sent,
            data=data,
            json=json,
            extensions={"sni_hostname": host.split(":", 1)[0]},
        )
        for line in response.headers.get_list("set-cookie"):
            parsed = SimpleCookie()
            parsed.load(line)
            for name, morsel in parsed.items():
                if morsel["max-age"] == "0" or not morsel.value.strip('"'):
                    jar.pop(name, None)
                else:
                    jar[name] = morsel.value
        return response

    def follow(self, url, response):
        location = response.headers["location"]
        if location.startswith("/"):
            location = f"https://{urlsplit(url).netloc}{location}"
        return location


def _extension(proj):
    root = proj / "extensions" / "acme-gate"
    package = root / "src" / "acme_gate"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (package / "__init__.py").write_text(PACKAGE)
    (package / "web.py").write_text(WEB)


def _hidden(page, name):
    return re.search(rf'name="{name}" value="([^"]+)"', page.text)[1]


def _images(registry):
    """Tag this checkout's base images under a registry no other run uses, so a concurrent
    `dev build-images` elsewhere cannot swap the MCP the project image is built from."""
    for name in ("vecten-airflow", "vecten-mcp"):
        source = f"{name}:{VERSIONS.vecten}"
        subprocess.run(["docker", "tag", source, f"{registry}/{source}"], check=True)
    probe = subprocess.run(
        ["docker", "run", "--rm", f"{registry}/vecten-mcp:{VERSIONS.vecten}",
         "python", "-c", "import vecten_mcp.auth.gate"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert probe.returncode == 0, "the MCP base image lacks the gate: " + probe.stderr


def _sign_in(browser):
    """From a login route through the gate's start and the server's sign-in page; the
    answer to the posted credentials."""
    first = browser.go("GET", f"https://{WEB_HOST}/docs?page=2")
    assert first.status_code == 302
    start = browser.follow(f"https://{WEB_HOST}/", first)
    assert start == f"https://{WEB_HOST}/_vecten/gate/start?rd=/docs?page=2"
    started = browser.go("GET", start)
    assert started.status_code == 303, started.text
    authorize = started.headers["location"]
    assert authorize.startswith(f"https://{MCP_HOST}/gate/authorize?")
    page = browser.go("GET", authorize)
    assert page.status_code == 200, page.text
    return browser.go(
        "POST",
        f"https://{MCP_HOST}/login",
        data={
            "flow": _hidden(page, "flow"),
            "csrf": _hidden(page, "csrf"),
            "email": ADMIN,
            "password": PASSWORD,
        },
    )


def _provision(admin):
    """`vecten user add` over the admin API, with a token from the authorization code flow:
    the first admin is invited at start and has no warehouse account until then."""
    redirect = "http://127.0.0.1:9000/cb"
    client = admin.go(
        "POST",
        f"https://{MCP_HOST}/register",
        json={"client_name": "gate test", "redirect_uris": [redirect]},
    ).json()
    verifier = "v" * 43
    digest = hashlib.sha256(verifier.encode()).digest()
    resource = f"https://{MCP_HOST}/mcp"
    query = {
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "resource": resource,
        "code_challenge": base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
        "code_challenge_method": "S256",
        "state": "s",
    }
    page = admin.go("GET", f"https://{MCP_HOST}/authorize?" + urlencode(query))
    flow, csrf = _hidden(page, "flow"), _hidden(page, "csrf")
    admin.go(
        "POST",
        f"https://{MCP_HOST}/login",
        data={"flow": flow, "csrf": csrf, "email": ADMIN, "password": PASSWORD},
    )
    approved = admin.go(
        "POST",
        f"https://{MCP_HOST}/consent",
        data={"flow": flow, "csrf": csrf, "decision": "approve"},
    )
    target = unescape(re.search(r'<a href="([^"]+)"', approved.text)[1])
    tokens = admin.go(
        "POST",
        f"https://{MCP_HOST}/token",
        data={
            "grant_type": "authorization_code",
            "code": parse_qs(urlsplit(target).query)["code"][0],
            "client_id": client["client_id"],
            "redirect_uri": redirect,
            "code_verifier": verifier,
            "resource": resource,
        },
    ).json()
    created = admin.go(
        "POST",
        f"https://{MCP_HOST}/api/admin/users/create",
        json={"email": ADMIN, "name": "Ada"},
        headers={"Authorization": "Bearer " + tokens["access_token"]},
    )
    assert created.status_code == 200, created.text


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
def test_a_person_signs_in_once_and_the_service_sees_who_they_are(tmp_path, monkeypatch):
    build = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", "dev", "build-images", "--repo", REPO, "--json"],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    registry = f"vecten-gate-{uuid.uuid4().hex[:8]}"
    _images(registry)
    monkeypatch.setenv("VECTEN_IMAGE_REGISTRY", registry)
    init = ("--name", "acme", "--users", "3", "--source", "hubspot", "--json")
    assert vecten("init", "p", *init, cwd=tmp_path).returncode == 0
    proj = tmp_path / "p"
    toml = proj / "vecten.toml"
    toml.write_text(toml.read_text().replace("admins = []", f'admins = ["{ADMIN}"]'))
    token = secrets.token_urlsafe(24)
    LocalProvider(proj).put_secret("MCP_ADMIN_SETUP_TOKEN", token)
    project = compose_project_name(proj, load_config(proj))
    drop_volumes(project)
    _extension(proj)
    added = vecten("extension", "add", "./extensions/acme-gate", "--json", cwd=proj)
    assert added.returncode == 0, added.stdout + added.stderr
    routes = load_config(proj).services["acme-gate"]["web"].routes
    assert routes["books"].groups == ["finance"] and routes["home"].access == "login"
    try:
        up = vecten("up", "--json", cwd=proj)
        assert up.returncode == 0, up.stdout + up.stderr
        assert json.loads(up.stdout)["routes"]["acme-gate.web.home"] == f"https://{WEB_HOST}/"
        trusted = ssl.create_default_context()
        for name in ("web.localhost", "mcp.localhost"):
            trusted.load_verify_locations(proj / STACK_DIR / "certs" / f"{name}.crt")
        with httpx.Client(verify=trusted, timeout=30) as client:
            browser = Browser(client)
            setup = browser.go("GET", f"https://{MCP_HOST}/set-password")
            done = browser.go(
                "POST",
                f"https://{MCP_HOST}/set-password",
                data={"csrf": _hidden(setup, "csrf"), "token": token, "password": PASSWORD},
            )
            assert done.status_code == 200, done.text

            # Signed in, but without a warehouse account yet: no portal.
            refused = _sign_in(browser)
            assert refused.status_code == 400 and "Ask an admin" in refused.text
            _provision(Browser(client))
            signed = _sign_in(browser)
            callback = unescape(re.search(r'<a href="([^"]+)"', signed.text)[1])
            assert callback.startswith(f"https://{WEB_HOST}/_vecten/gate/callback?code=")
            landed = browser.go("GET", callback)
            assert landed.status_code == 303 and landed.headers["location"] == "/docs?page=2"
            assert "__Host-vecten-gate" in browser.jars[WEB_HOST]
            assert "__Host-vecten-gate" not in browser.jars[MCP_HOST]
            assert "__Host-vecten-gate-state" not in browser.jars[WEB_HOST]

            # A sign-in left unfinished elsewhere keeps its state cookie; no service sees it.
            browser.jars[WEB_HOST]["__Host-vecten-gate-state"] = "v" * 43
            seen = browser.go("GET", f"https://{WEB_HOST}/docs?page=2", headers=FORGED)
            assert seen.status_code == 200, seen.text
            echoed = seen.json()
            assert echoed["x-vecten-user"] == ADMIN
            assert echoed["x-vecten-groups"] == "default"
            assert echoed["x-vecten-admin"] == "true"
            assert echoed["x-vecten-principal"] and echoed["x-vecten-name"]
            assert "vecten-gate" not in echoed.get("cookie", "")
            browser.jars[WEB_HOST].pop("__Host-vecten-gate-state")

            refused = browser.go("GET", f"https://{WEB_HOST}/books")
            assert refused.status_code == 403 and "No access" in refused.text

            hook = browser.go("POST", f"https://{WEB_HOST}/hook", headers=FORGED)
            assert hook.status_code == 200
            assert not [h for h in hook.json() if h.startswith("x-vecten")]

            session = dict(browser.jars[WEB_HOST])
            asked = browser.go("GET", f"https://{WEB_HOST}/_vecten/gate/logout")
            assert asked.status_code == 200 and 'method="post"' in asked.text
            out = browser.go(
                "POST",
                f"https://{WEB_HOST}/_vecten/gate/logout",
                data={"csrf": _hidden(asked, "csrf")},
            )
            assert out.status_code == 200, out.text
            assert "__Host-vecten-gate" not in browser.jars[WEB_HOST]
            browser.jars[WEB_HOST] = session
            again = browser.go("GET", f"https://{WEB_HOST}/docs")
            assert again.status_code == 302
            # The server's own sign-in ended too: the next sign-in asks for credentials.
            started = browser.go("GET", f"https://{WEB_HOST}/_vecten/gate/start?rd=/")
            form = browser.go("GET", started.headers["location"])
            assert form.status_code == 200 and 'name="password"' in form.text
    finally:
        vecten("down", "--volumes", "--json", cwd=proj)
        drop_volumes(project)
        remove_stack_images(project)
        for name in ("vecten-airflow", "vecten-mcp"):
            subprocess.run(["docker", "rmi", f"{registry}/{name}:{VERSIONS.vecten}"],
                           capture_output=True)  # fmt: skip
        # `extension add` installs the extension into the Python running the CLI.
        subprocess.run(["uv", "pip", "uninstall", "--python", sys.executable, "acme-gate"])
