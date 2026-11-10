"""NGINX keeps answering after an upstream container comes back on a different address."""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest

from vecten.stack.certs import ensure_self_signed
from vecten.stack.nginx import write_nginx
from vecten_shared.versions import VERSIONS

pytestmark = pytest.mark.docker

HOST = "mcp.localhost"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def docker_ok() -> bool:
    return shutil.which("docker") is not None and docker("info", check=False).returncode == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def address(container: str) -> str:
    return docker(
        "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container
    ).stdout.strip()


def status(port: int, ca: Path) -> int | None:
    # SNI must name the server block; mcp.localhost resolves to the loopback address.
    try:
        return httpx.get(
            f"https://{HOST}:{port}/health", verify=ssl.create_default_context(cafile=ca), timeout=5
        ).status_code
    except httpx.HTTPError:
        return None


def wait_for(port: int, ca: Path, code: int, seconds: float) -> int | None:
    deadline = time.monotonic() + seconds
    last = status(port, ca)
    while last != code and time.monotonic() < deadline:
        time.sleep(1)
        last = status(port, ca)
    return last


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
def test_nginx_follows_an_upstream_that_moves_to_a_new_address(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    network, upstream = f"vecten-nginx-{suffix}", f"vecten-nginx-up-{suffix}"
    front, placeholder = f"vecten-nginx-front-{suffix}", f"vecten-nginx-hold-{suffix}"
    write_nginx(tmp_path, ["mcp"], {"mcp": HOST}, "local")
    ensure_self_signed(tmp_path, [HOST])
    ca = tmp_path / "certs" / f"{HOST}.crt"
    port = free_port()
    serve = "mkdir -p /srv && echo ok > /srv/health && exec python -m http.server 8001 -d /srv"
    try:
        docker("network", "create", network)
        docker(
            "run", "-d", "--name", upstream, "--network", network,
            "--network-alias", "mcp-server", VERSIONS.mcp_base_image, "sh", "-c", serve,
        )  # fmt: skip
        docker(
            "run", "-d", "--name", front, "--network", network,
            "-p", f"127.0.0.1:{port}:443",
            "-v", f"{tmp_path / 'nginx' / 'conf.d'}:/etc/nginx/conf.d:ro",
            "-v", f"{tmp_path / 'certs'}:/etc/nginx/certs:ro",
            VERSIONS.nginx_image,
        )  # fmt: skip
        assert wait_for(port, ca, 200, 60) == 200
        before = address(upstream)

        # A recreated container gets a fresh address while another container takes the old one.
        docker("network", "disconnect", network, upstream)
        docker(
            "run", "-d", "--name", placeholder, "--network", network,
            VERSIONS.mcp_base_image, "sleep", "300",
        )  # fmt: skip
        docker("network", "connect", "--alias", "mcp-server", network, upstream)
        assert address(upstream) != before

        assert wait_for(port, ca, 200, 30) == 200
    finally:
        for name in (front, placeholder, upstream):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", network, check=False)
