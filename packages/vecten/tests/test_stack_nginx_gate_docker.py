"""NGINX asks a stub gate about every login request; stubs log what reaches them."""

from __future__ import annotations

import importlib.util
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from vecten.stack.certs import ensure_self_signed
from vecten.stack.nginx import ServiceRoute, ServiceSite, write_nginx
from vecten_shared.versions import VERSIONS

spec = importlib.util.spec_from_file_location(
    "nginx_gate_docker_services", Path(__file__).with_name("test_stack_nginx_services_docker.py")
)
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)
util = services.util
request, seen, STUB = services.request, services.seen, services.STUB
pytestmark = pytest.mark.docker

HOST = services.HOST
BUILTIN = services.BUILTIN
MCP = "mcp.gate.test"
# Answers the gate's paths: a session `good` is Ada in `default`, `outsider` is outside every
# route's groups, anything else has no session. Logs one JSON line per request.
GATE = """
import json, ssl, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Gate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        headers = {k.lower(): v for k, v in self.headers.items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        print(json.dumps({"method": self.command, "path": self.path, "headers": headers,
                          "body": body}), flush=True)
        cookie = headers.get("cookie", "")
        status, extra = 404, {}
        if self.path == "/gate/verify":
            if cookie == "__Host-vecten-gate=good":
                status, extra = 200, {
                    "X-Vecten-User": "ada@acme.com", "X-Vecten-Principal": "p-ada",
                    "X-Vecten-Name": "Ada", "X-Vecten-Groups": "default",
                    "X-Vecten-Admin": "false",
                }
            elif cookie == "__Host-vecten-gate=outsider":
                status = 403
            else:
                status = 401
        elif self.path.startswith("/gate/"):
            status, extra = 303, {"Location": "https://" + headers.get("host", "") + "/next"}
        self.send_response(status)
        for name, value in extra.items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *args):
        pass

server = ThreadingHTTPServer(("", int(sys.argv[1])), Gate)
if len(sys.argv) > 2:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(sys.argv[2], sys.argv[3])
    server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
"""
SITE = ServiceSite(
    host=HOST,
    upstream="http://svc-acme-bot:8000",
    health_path="/healthz",
    routes=(
        ServiceRoute("/", "login", groups=("default",), websocket=True, key="acme.bot.home"),
        ServiceRoute("/books", "login", groups=("finance",), key="acme.bot.books"),
        # A documentation network no test client sits in.
        ServiceRoute(
            "/office", "login", groups=("default",), allow=("192.0.2.0/24",), key="acme.bot.office"
        ),
        ServiceRoute("/hook", "public", key="acme.bot.hook"),
    ),
)


def _letsencrypt(tmp_path: Path, names: list[str]) -> Path:
    certs = ensure_self_signed(tmp_path, names)
    root = tmp_path / "letsencrypt"
    for name in names:
        live = root / "live" / name
        live.mkdir(parents=True)
        shutil.copyfile(certs / f"{name}.crt", live / "fullchain.pem")
        shutil.copyfile(certs / f"{name}.key", live / "privkey.pem")
    return root


def head(port, ca, method, path, headers="", body=""):
    raw = request(
        port,
        ca,
        f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\n{headers}"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}",
    )
    lines = raw.split("\r\n")
    found = {}
    for line in lines[1:]:
        if ": " in line:
            name, value = line.split(": ", 1)
            found[name.lower()] = value
    return int(lines[0].split(" ", 2)[1]), found, raw


@contextmanager
def gate_behind_nginx(tmp_path, *, cross_vm: bool, trusted: bool = True):
    suffix = uuid.uuid4().hex[:8]
    network = f"vecten-gate-{suffix}"
    stub, gate, front = (f"vecten-gate-{role}-{suffix}" for role in ("stub", "mcp", "front"))
    if cross_vm:
        components, hosts = ["platform"], {"airflow": BUILTIN, "mcp": MCP}
    else:
        components, hosts = ["mcp"], {"mcp": MCP}
    write_nginx(tmp_path, components, hosts, "vm", services=[SITE])
    letsencrypt = _letsencrypt(tmp_path, [HOST, BUILTIN, MCP])
    certs = tmp_path / "certs"
    ca = certs / f"{HOST}.crt"
    # NGINX trusts the gate's own certificate, or one that did not sign it.
    bundle = certs / (f"{MCP}.crt" if trusted else f"{BUILTIN}.crt")
    port = util.free_port()
    try:
        util.docker("network", "create", network)
        util.docker(
            "run", "-d", "--name", stub, "--network", network,
            "--network-alias", "svc-acme-bot", VERSIONS.mcp_base_image, "python", "-c", STUB,
        )  # fmt: skip
        if cross_vm:
            util.docker(
                "run", "-d", "--name", gate, "--network", network, "--network-alias", MCP,
                "-v", f"{certs}:/certs:ro", VERSIONS.mcp_base_image, "python", "-c", GATE,
                "443", f"/certs/{MCP}.crt", f"/certs/{MCP}.key",
            )  # fmt: skip
        else:
            util.docker(
                "run", "-d", "--name", gate, "--network", network,
                "--network-alias", "mcp-server", VERSIONS.mcp_base_image, "python", "-c", GATE,
                "8001",
            )  # fmt: skip
        util.docker(
            "run", "-d", "--name", front, "--network", network,
            "-p", f"127.0.0.1:{port}:443",
            "-v", f"{tmp_path / 'nginx' / 'conf.d'}:/etc/nginx/conf.d:ro",
            "-v", f"{letsencrypt}:/etc/letsencrypt:ro",
            "-v", f"{bundle}:/etc/ssl/certs/ca-certificates.crt:ro",
            VERSIONS.nginx_image,
        )  # fmt: skip
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if head(port, ca, "GET", "/_vecten/health")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        yield port, ca, stub, gate
    finally:
        for name in (front, gate, stub):
            util.docker("rm", "-f", name, check=False)
        util.docker("network", "rm", network, check=False)


FORGED = (
    "X-Vecten-User: mallory@evil.example\r\nx-vecten-groups: finance\r\n"
    "X-VECTEN-ADMIN: true\r\nX_Vecten_User: mallory@evil.example\r\n"
)


@pytest.mark.skipif(not util.docker_ok(), reason="no docker daemon")
@pytest.mark.parametrize("cross_vm", [False, True], ids=["compose-network", "verified-https"])
def test_nginx_admits_a_login_request_only_on_the_gates_word(tmp_path, cross_vm):
    with gate_behind_nginx(tmp_path, cross_vm=cross_vm) as (port, ca, stub, gate):
        status, headers, _ = head(port, ca, "GET", "/docs?x=1&y=%2Fz")
        assert status == 302
        assert headers["location"] == "/_vecten/gate/start?rd=/docs?x=1&y=%2Fz"

        cookies = "Cookie: a=1; vecten-gate-state=V; __Host-vecten-gate=none\r\n"
        status, headers, _ = head(port, ca, "GET", "/_vecten/gate/start?rd=/docs?x=1", cookies)
        assert status == 303

        good = "Cookie: a=1; __Host-vecten-gate=good; b=2\r\n"
        assert head(port, ca, "GET", "/docs", FORGED + good)[0] == 200
        assert head(port, ca, "POST", "/docs/form", good, body="field=1")[0] == 200
        upgrade = request(
            port,
            ca,
            f"GET /live HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n{good}\r\n",
        )
        assert upgrade.startswith("HTTP/1.1 101"), upgrade

        status, headers, raw = head(
            port, ca, "GET", "/books", "Cookie: __Host-vecten-gate=outsider\r\n"
        )
        assert status == 403 and "No access" in raw and "/_vecten/gate/logout" in raw
        before = len(seen(gate))
        # Refused before the gate is asked: an address outside `allow`, a repeated cookie, a
        # path NGINX would read otherwise, and the internal check called from outside.
        assert head(port, ca, "GET", "/office", good)[0] == 403
        decoy = "Cookie: __HOST-VECTEN-GATE=decoy; __Host-vecten-gate=good\r\n"
        assert head(port, ca, "GET", "/docs", decoy)[0] == 400
        assert head(port, ca, "GET", "/docs/..%2Fbooks", good)[0] == 400
        assert head(port, ca, "GET", "/_vecten/gate/verify/acme.bot.home", good)[0] == 404
        assert len(seen(gate)) == before
        assert head(port, ca, "POST", "/hook", FORGED + good)[0] == 200
        asked, received = seen(gate), [r for r in seen(stub) if r["path"] != "/healthz"]

    starts = [r for r in asked if r["path"].startswith("/gate/start")]
    assert [r["path"] for r in starts] == ["/gate/start?rd=/docs?x=1"]
    assert starts[0]["headers"]["x-vecten-gate-host"] == HOST
    assert starts[0]["headers"]["cookie"] == "__Host-vecten-gate=none; vecten-gate-state=V"
    assert starts[0]["headers"]["host"] == MCP
    checks = [r for r in asked if r["path"] == "/gate/verify"]
    assert {r["headers"]["x-vecten-gate-route"] for r in checks} == {
        "acme.bot.home",
        "acme.bot.books",
    }
    for check in checks:
        assert check["method"] == "GET" and check["body"] == ""
        assert check["headers"]["x-vecten-gate-host"] == HOST
        assert "a=1" not in check["headers"].get("cookie", "")

    assert [(r["method"], r["path"]) for r in received] == [
        ("GET", "/docs"),
        ("POST", "/docs/form"),
        ("GET", "/live"),
        ("POST", "/hook"),
    ]
    for answered in received[:3]:
        sent = answered["headers"]
        assert sent["x-vecten-user"] == "ada@acme.com"
        assert sent["x-vecten-principal"] == "p-ada"
        assert sent["x-vecten-groups"] == "default"
        assert sent["x-vecten-admin"] == "false"
        assert sent["cookie"] == "a=1; b=2"
        assert "x_vecten_user" not in sent
    hook = received[-1]["headers"]
    assert not [h for h in hook if h.replace("_", "-").startswith("x-vecten")], hook


@pytest.mark.skipif(not util.docker_ok(), reason="no docker daemon")
def test_a_gate_whose_certificate_nginx_cannot_verify_admits_nobody(tmp_path):
    with gate_behind_nginx(tmp_path, cross_vm=True, trusted=False) as (port, ca, stub, gate):
        good = "Cookie: __Host-vecten-gate=good\r\n"
        assert head(port, ca, "GET", "/docs", good)[0] == 500
        assert head(port, ca, "GET", "/_vecten/gate/start?rd=/", good)[0] == 502
        assert seen(gate) == []
        assert [r for r in seen(stub) if r["path"] != "/healthz"] == []
