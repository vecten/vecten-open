"""The pinned NGINX image starts on the front door Vecten writes, whatever the hostnames' length."""

import subprocess

import pytest

from vecten.stack.certs import ensure_self_signed
from vecten.stack.nginx import write_nginx
from vecten_shared.versions import VERSIONS

from .docker_util import docker_available

pytestmark = pytest.mark.docker


def test_nginx_accepts_hostnames_longer_than_its_default_bucket_holds(tmp_path):
    if not docker_available():
        pytest.skip("Docker is not available")
    # A self-signed certificate's common name holds at most 64 characters; 60 is past the
    # 46 that NGINX's default 64-byte server-name bucket holds on a 64-bit host.
    stem = "x" * (60 - len("airflow..example.com"))
    hosts = {"airflow": f"airflow.{stem}.example.com", "mcp": f"mcp.{stem}.example.com"}
    write_nginx(tmp_path, ["platform", "mcp"], hosts, "local")
    ensure_self_signed(tmp_path, list(hosts.values()))
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{tmp_path / 'nginx' / 'conf.d'}:/etc/nginx/conf.d:ro",
            "-v",
            f"{tmp_path / 'certs'}:/etc/nginx/certs:ro",
            VERSIONS.nginx_image,
            "nginx",
            "-t",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
