import json
import os
import subprocess
import sys
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

from vecten.stack.compose import compose_project_name
from vecten_shared.config import load_config
from vecten_shared.identity_setup import provisioner_script
from vecten_shared.versions import VERSIONS

from .docker_util import remove_stack_images

pytestmark = pytest.mark.docker
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def docker_ok() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def project_volumes(project: str) -> list[str]:
    label = f"label=com.docker.compose.project={project}"
    return subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", label], capture_output=True, text=True
    ).stdout.split()


def drop_volumes(project: str) -> None:
    # `down` keeps volumes, and a fresh .env would meet the previous run's Postgres passwords.
    names = project_volumes(project)
    if names:
        subprocess.run(["docker", "volume", "rm", *names], capture_output=True, check=True)


def front_door(client, path, host, **kwargs):
    # A browser on https://<host> sends the bare name as SNI; NGINX refuses a handshake without one.
    return client.get(
        f"https://127.0.0.1:8443{path}",
        headers={"Host": host},
        extensions={"sni_hostname": host.split(":", 1)[0]},
        **kwargs,
    )


def service_container(project: str, service: str) -> str:
    labels = [
        f"label=com.docker.compose.project={project}",
        f"label=com.docker.compose.service={service}",
    ]
    cmd = ["docker", "ps", "-q"] + [arg for label in labels for arg in ("--filter", label)]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def vecten(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", *args], cwd=cwd, capture_output=True, text=True
    )


def add_existing_warehouse(proj):
    # MCP validates warehouse access at startup. Exercise onboarding against a separate
    # database instead of a placeholder hostname or the stack's managed warehouse.
    init = proj / "existing-warehouse-init"
    init.mkdir(mode=0o755)
    script = init / "10-identity.sql"
    script.write_text(
        "CREATE SCHEMA core;\nCREATE SCHEMA marts;\n"
        + provisioner_script("postgres", database="dw", issuer="https://mcp.localhost:8443")
    )
    script.chmod(0o644)
    override = {
        "services": {
            "existing-warehouse": {
                "image": f"postgres:{VERSIONS.postgres_major}",
                "profiles": ["mcp"],
                "environment": {
                    "POSTGRES_DB": "dw",
                    "POSTGRES_PASSWORD": "fixture-master",
                    "MCP_PROVISIONER_PASSWORD": "${PROVISIONER_PASSWORD}",
                    "MCP_IMPERSONATOR_PASSWORD": "${IMPERSONATOR_PASSWORD}",
                },
                "volumes": [
                    "existing-warehouse-data:/var/lib/postgresql/data",
                    f"{init}:/docker-entrypoint-initdb.d:ro",
                ],
                "healthcheck": {
                    "test": ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "dw"],
                    "interval": "5s",
                    "timeout": "5s",
                    "retries": 20,
                },
            },
            "mcp-server": {"depends_on": {"existing-warehouse": {"condition": "service_healthy"}}},
        },
        "volumes": {"existing-warehouse-data": {}},
    }
    (proj / "orchestration" / "docker-compose.override.yml").write_text(yaml.safe_dump(override))


AIRFLOW_SERVICES = (
    "airflow-api-server",
    "airflow-scheduler",
    "airflow-dag-processor",
    "airflow-triggerer",
)


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
@pytest.mark.parametrize(
    "init_args,expect_airflow",
    [
        (("--name", "acme", "--users", "3", "--source", "hubspot"), True),
        (
            (
                "--name",
                "acme",
                "--users",
                "3",
                "--has-warehouse",
                "yes",
                "--warehouse-url",
                "postgres://existing-warehouse/dw",
            ),
            False,
        ),
    ],
)
def test_init_up_check_down_twice(tmp_path, init_args, expect_airflow):
    build = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", "dev", "build-images", "--repo", REPO, "--json"],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    assert vecten("init", "p", *init_args, "--json", cwd=tmp_path).returncode == 0
    proj = tmp_path / "p"
    if not expect_airflow:
        add_existing_warehouse(proj)
    project = compose_project_name(proj, load_config(proj))
    drop_volumes(project)
    try:
        r = vecten("up", "--json", cwd=proj)
        assert r.returncode == 0, r.stdout + r.stderr
        urls = json.loads(r.stdout)["urls"]
        with httpx.Client(verify=False, timeout=10) as client:
            if expect_airflow:
                # Airflow's own login guards it: the page loads, the API refuses a stranger.
                assert front_door(client, "/", "airflow.localhost:8443").status_code == 200
                dags = front_door(client, "/api/v2/dags", "airflow.localhost:8443")
                assert dags.status_code == 401
                # Airflow 3.1 issues no trailing-slash redirects; its auth redirect is the one
                # whose Location must keep the https scheme and the published port.
                login = front_door(client, "/api/v2/auth/login", "airflow.localhost:8443")
                assert login.is_redirect, login.status_code
                location = login.headers["location"]
                assert not location.startswith("http://"), location
                parts = urlsplit(location)
                relative = not parts.scheme and not parts.netloc
                assert relative or location.startswith("https://airflow.localhost:8443/"), location
                for service in AIRFLOW_SERVICES:
                    container = service_container(project, service)
                    probe = subprocess.run(
                        [
                            "docker",
                            "exec",
                            container,
                            "sh",
                            "-c",
                            "id -u; f=/opt/airflow/logs/.uid-probe; "
                            "touch $f && stat -c %u $f; rm $f",
                        ],
                        capture_output=True,
                        text=True,
                    )
                    assert probe.stdout.split() == [str(os.getuid())] * 2, (service, probe)
            else:
                assert "airflow" not in urls
            assert front_door(client, "/health", "mcp.localhost:8443").status_code == 200
            with pytest.raises(httpx.HTTPError):
                front_door(client, "/", "other.localhost")
    finally:
        try:
            assert vecten("down", "--json", cwd=proj).returncode == 0
            again = vecten("down", "--json", cwd=proj)
            assert again.returncode == 0 and json.loads(again.stdout)["stopped"] is False
        finally:
            cleanup = vecten("down", "--volumes", "--json", cwd=proj)
            left = project_volumes(project)
            drop_volumes(project)
            remove_stack_images(project)
            assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr
            assert json.loads(cleanup.stdout)["volumes_removed"] is True
            assert left == []
