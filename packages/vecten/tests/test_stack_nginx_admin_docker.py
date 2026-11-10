"""NGINX narrows who reaches /admin while the MCP endpoint stays public."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest

from vecten.stack.certs import ensure_self_signed
from vecten.stack.nginx import write_nginx
from vecten_shared.versions import VERSIONS

spec = importlib.util.spec_from_file_location(
    "nginx_admin_docker_util", Path(__file__).with_name("test_stack_nginx_upstream_docker.py")
)
util = importlib.util.module_from_spec(spec)
spec.loader.exec_module(util)
pytestmark = pytest.mark.docker
HOST = util.HOST


def statuses(port, ca):
    import ssl

    import httpx

    context = ssl.create_default_context(cafile=ca)
    return {
        path: httpx.get(f"https://{HOST}:{port}{path}", verify=context, timeout=5).status_code
        for path in ("/health", "/admin/login")
    }


@pytest.mark.skipif(not util.docker_ok(), reason="no docker daemon")
@pytest.mark.parametrize(
    ("settings", "admin_status"),
    [
        ({}, 200),
        ({"admin_allow": ["192.0.2.0/24"]}, 403),
        ({"admin_panel": "off"}, 404),
    ],
)
def test_admin_exposure_is_enforced_by_nginx(tmp_path, settings, admin_status):
    suffix = uuid.uuid4().hex[:8]
    network, upstream, front = (
        f"vecten-admin-{suffix}",
        f"vecten-admin-up-{suffix}",
        f"vecten-admin-front-{suffix}",
    )
    write_nginx(tmp_path, ["mcp"], {"mcp": HOST}, "local", **settings)
    ensure_self_signed(tmp_path, [HOST])
    ca = tmp_path / "certs" / f"{HOST}.crt"
    port = util.free_port()
    serve = (
        "mkdir -p /srv/admin && echo ok > /srv/health && echo panel > /srv/admin/login "
        "&& exec python -m http.server 8001 -d /srv"
    )
    try:
        util.docker("network", "create", network)
        util.docker(
            "run", "-d", "--name", upstream, "--network", network,
            "--network-alias", "mcp-server", VERSIONS.mcp_base_image, "sh", "-c", serve,
        )  # fmt: skip
        util.docker(
            "run", "-d", "--name", front, "--network", network,
            "-p", f"127.0.0.1:{port}:443",
            "-v", f"{tmp_path / 'nginx' / 'conf.d'}:/etc/nginx/conf.d:ro",
            "-v", f"{tmp_path / 'certs'}:/etc/nginx/certs:ro",
            VERSIONS.nginx_image,
        )  # fmt: skip
        assert util.wait_for(port, ca, 200, 60) == 200
        assert statuses(port, ca) == {"/health": 200, "/admin/login": admin_status}
    finally:
        for name in (front, upstream):
            util.docker("rm", "-f", name, check=False)
        util.docker("network", "rm", network, check=False)
