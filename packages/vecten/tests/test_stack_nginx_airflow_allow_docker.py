"""NGINX narrows who reaches Airflow while the health path the ship polls stays open."""

from __future__ import annotations

import importlib.util
import ssl
import time
import uuid
from pathlib import Path

import pytest

from vecten.stack.certs import ensure_self_signed
from vecten.stack.nginx import AIRFLOW_HEALTH_PATH, write_nginx
from vecten_shared.versions import VERSIONS

spec = importlib.util.spec_from_file_location(
    "nginx_airflow_docker_util", Path(__file__).with_name("test_stack_nginx_upstream_docker.py")
)
util = importlib.util.module_from_spec(spec)
spec.loader.exec_module(util)
pytestmark = pytest.mark.docker
HOST = "airflow.localhost"


def statuses(port, ca):
    import httpx

    context = ssl.create_default_context(cafile=ca)
    answers = {}
    for path in (AIRFLOW_HEALTH_PATH, "/auth/token"):
        try:
            response = httpx.get(f"https://{HOST}:{port}{path}", verify=context, timeout=5)
            answers[path] = response.status_code
        except httpx.HTTPError:
            answers[path] = None
    return answers


@pytest.mark.skipif(not util.docker_ok(), reason="no docker daemon")
@pytest.mark.parametrize(
    ("allow", "login_status"),
    [
        ([], 200),
        # A documentation network no test client sits in.
        (["192.0.2.0/24"], 403),
        (["0.0.0.0/0", "::/0"], 200),
    ],
)
def test_airflow_allow_list_is_enforced_by_nginx(tmp_path, allow, login_status):
    suffix = uuid.uuid4().hex[:8]
    network, upstream, front = (
        f"vecten-airflow-{suffix}",
        f"vecten-airflow-up-{suffix}",
        f"vecten-airflow-front-{suffix}",
    )
    write_nginx(tmp_path, ["platform"], {"airflow": HOST}, "local", airflow_allow=allow)
    ensure_self_signed(tmp_path, [HOST])
    ca = tmp_path / "certs" / f"{HOST}.crt"
    port = util.free_port()
    serve = (
        "mkdir -p /srv/api/v2/monitor /srv/auth && echo ok > /srv/api/v2/monitor/health "
        "&& echo token > /srv/auth/token && exec python -m http.server 8080 -d /srv"
    )
    try:
        util.docker("network", "create", network)
        util.docker(
            "run", "-d", "--name", upstream, "--network", network,
            "--network-alias", "airflow-api-server", VERSIONS.mcp_base_image, "sh", "-c", serve,
        )  # fmt: skip
        util.docker(
            "run", "-d", "--name", front, "--network", network,
            "-p", f"127.0.0.1:{port}:443",
            "-v", f"{tmp_path / 'nginx' / 'conf.d'}:/etc/nginx/conf.d:ro",
            "-v", f"{tmp_path / 'certs'}:/etc/nginx/certs:ro",
            VERSIONS.nginx_image,
        )  # fmt: skip
        deadline = time.monotonic() + 60
        answers = statuses(port, ca)
        while answers[AIRFLOW_HEALTH_PATH] != 200 and time.monotonic() < deadline:
            time.sleep(1)
            answers = statuses(port, ca)
        assert answers == {AIRFLOW_HEALTH_PATH: 200, "/auth/token": login_status}
    finally:
        for name in (front, upstream):
            util.docker("rm", "-f", name, check=False)
        util.docker("network", "rm", network, check=False)
