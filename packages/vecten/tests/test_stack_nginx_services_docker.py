"""NGINX in front of a service: a stub upstream logs every request it receives."""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import ssl
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from vecten.stack.certs import ensure_self_signed
from vecten.stack.nginx import ServiceRoute, ServiceSite, write_nginx
from vecten_shared.versions import VERSIONS

spec = importlib.util.spec_from_file_location(
    "nginx_services_docker_util", Path(__file__).with_name("test_stack_nginx_upstream_docker.py")
)
util = importlib.util.module_from_spec(spec)
spec.loader.exec_module(util)
pytestmark = pytest.mark.docker

HOST = "bot.localhost"
BUILTIN = "airflow.localhost"
# Prints one JSON line per request; answers a WebSocket handshake with 101.
STUB = """
import base64, hashlib, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def answer(self):
        headers = {k.lower(): v for k, v in self.headers.items()}
        print(json.dumps({"method": self.command, "path": self.path, "headers": headers}),
              flush=True)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.headers.get("Upgrade", "").lower() == "websocket":
            key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b"ok")

    do_GET = do_POST = do_HEAD = answer

    def log_message(self, *args):
        pass

ThreadingHTTPServer(("", 8000), Stub).serve_forever()
"""
SITE = ServiceSite(
    host=HOST,
    upstream="http://svc-acme-bot:8000",
    health_path="/healthz",
    routes=(
        ServiceRoute("/api/messages", "public"),
        ServiceRoute("/live", "public", websocket=True),
        # A documentation network no test client sits in.
        ServiceRoute("/office", "public", allow=("192.0.2.0/24",)),
        ServiceRoute("/settings", "login", groups=("finance",)),
    ),
)


def _letsencrypt(tmp_path: Path) -> Path:
    """Self-signed certificates where a VM keeps Let's Encrypt's."""
    certs = ensure_self_signed(tmp_path, [HOST, BUILTIN])
    root = tmp_path / "letsencrypt"
    for name in (HOST, BUILTIN):
        live = root / "live" / name
        live.mkdir(parents=True)
        shutil.copyfile(certs / f"{name}.crt", live / "fullchain.pem")
        shutil.copyfile(certs / f"{name}.key", live / "privkey.pem")
    return root


def request(port: int, ca: Path, raw: str) -> str:
    """Send `raw` as written, so no client tidies its path; the status line and headers."""
    context = ssl.create_default_context(cafile=ca)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=HOST) as tls:
            tls.sendall(raw.encode("latin-1"))
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
    return data.decode("latin-1")


def status(port: int, ca: Path, method: str, path: str, headers: str = "") -> int:
    head = request(
        port,
        ca,
        f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\n{headers}Content-Length: 0\r\n"
        "Connection: close\r\n\r\n",
    )
    return int(head.split(" ", 2)[1])


def seen(stub: str) -> list[dict]:
    lines = util.docker("logs", stub).stdout.splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


@contextmanager
def service_behind_nginx(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    network, stub, front = (
        f"vecten-svc-{suffix}",
        f"vecten-svc-stub-{suffix}",
        f"vecten-svc-front-{suffix}",
    )
    write_nginx(tmp_path, ["platform"], {"airflow": BUILTIN}, "vm", services=[SITE])
    letsencrypt = _letsencrypt(tmp_path)
    ca = tmp_path / "certs" / f"{HOST}.crt"
    port = util.free_port()
    try:
        util.docker("network", "create", network)
        util.docker(
            "run", "-d", "--name", stub, "--network", network,
            "--network-alias", "svc-acme-bot", VERSIONS.mcp_base_image, "python", "-c", STUB,
        )  # fmt: skip
        util.docker(
            "run", "-d", "--name", front, "--network", network,
            "-p", f"127.0.0.1:{port}:443",
            "-v", f"{tmp_path / 'nginx' / 'conf.d'}:/etc/nginx/conf.d:ro",
            "-v", f"{letsencrypt}:/etc/letsencrypt:ro",
            VERSIONS.nginx_image,
        )  # fmt: skip
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if status(port, ca, "GET", "/_vecten/health") == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        yield port, ca, stub
    finally:
        for name in (front, stub):
            util.docker("rm", "-f", name, check=False)
        util.docker("network", "rm", network, check=False)


@pytest.mark.skipif(not util.docker_ok(), reason="no docker daemon")
def test_nginx_serves_a_service_hostname_as_the_routes_and_the_health_path_say(tmp_path):
    forged = (
        "X-Vecten-User: ada@acme.com\r\nx-vecten-groups: finance\r\n"
        "X-VECTEN-ADMIN: true\r\nX-Vecten-Principal: p1\r\nX-Vecten-Name: Ada\r\n"
        "X_Vecten_User: ada@acme.com\r\n"
        "Cookie: a=1; __Host-vecten-gate=secret; b=2\r\n"
    )
    with service_behind_nginx(tmp_path) as (port, ca, stub):
        before = len(seen(stub))
        assert status(port, ca, "POST", "/api/messages", forged) == 200
        assert status(port, ca, "GET", "/api/messages/thread/1", forged) == 200
        # A plain prefix would also cover these; whole segments do not.
        assert status(port, ca, "GET", "/api/messagesx") == 404
        assert status(port, ca, "GET", "/internal/debug") == 404
        assert status(port, ca, "GET", "/_vecten/anything") == 404
        assert status(port, ca, "HEAD", "/_vecten/health") == 200
        assert status(port, ca, "POST", "/_vecten/health") == 403
        assert status(port, ca, "GET", "/api/messages/..%2F..%2F_vecten%2Fhealth") == 200
        # The location is chosen by the normalised path, which these forms would hide.
        assert status(port, ca, "GET", "/api/messages/..%2F..%2Foffice") == 400
        assert status(port, ca, "GET", "/api/messages/%2e%2e/%2e%2e/office") == 400
        assert status(port, ca, "GET", "/api/messages/../../office") == 400
        assert status(port, ca, "GET", "/api/messages/..;/office") == 400
        assert status(port, ca, "GET", "/office") == 403
        decoy = "Cookie: __HOST-VECTEN-GATE=decoy; __Host-vecten-gate=REAL\r\n"
        assert status(port, ca, "GET", "/api/messages", decoy) == 400
        assert status(port, ca, "GET", "/_vecten/health", decoy) == 400
        assert status(port, ca, "GET", "/settings") == 503
        upgrade = request(
            port,
            ca,
            f"GET /live HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n",
        )
        assert upgrade.startswith("HTTP/1.1 101"), upgrade
        received = seen(stub)[before:]
    assert [(r["method"], r["path"]) for r in received] == [
        ("POST", "/api/messages"),
        ("GET", "/api/messages/thread/1"),
        ("HEAD", "/healthz"),
        ("GET", "/healthz"),
        ("GET", "/live"),
    ]
    for answered in received[:2]:
        headers = answered["headers"]
        assert not [h for h in headers if h.replace("_", "-").startswith("x-vecten")], headers
        assert headers["cookie"] == "a=1; b=2"
        assert headers["x-forwarded-for"]
    assert received[-1]["headers"]["upgrade"].lower() == "websocket"
