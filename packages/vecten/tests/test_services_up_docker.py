"""`vecten up` runs an in-repo extension's services beside the stack, isolated from it."""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from vecten.stack.compose import STACK_DIR, compose_project_name
from vecten_shared.config import load_config

from .test_up_e2e import REPO, docker_ok, drop_volumes, front_door, service_container, vecten

pytestmark = pytest.mark.docker

PYPROJECT = """\
[project]
name = "acme-svc"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = []
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"
[project.entry-points."vecten.services"]
web = "acme_svc:web"
crash = "acme_svc:crash"
"""
PACKAGE = """\
web = {
    "command": ["python", "-m", "acme_svc.web"],
    "port": 8000,
    "health": {"path": "/healthz"},
    "volume": "/var/lib/acme-svc",
}
crash = {
    "command": ["python", "-c", "import sys; sys.exit(3)"],
    "health": {"command": ["false"]},
}
"""
# Each start appends a line to the volume, so the count after a recreate proves the data stayed.
WEB = """\
import http.server
from pathlib import Path

boots = Path("/var/lib/acme-svc/boots")
with boots.open("a") as stream:
    stream.write("boot\\n")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = str(len(boots.read_text().splitlines())).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


http.server.ThreadingHTTPServer(("", 8000), Handler).serve_forever()
"""


def _extension(proj):
    root = proj / "extensions" / "acme-svc"
    package = root / "src" / "acme_svc"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (package / "__init__.py").write_text(PACKAGE)
    (package / "web.py").write_text(WEB)


def _health(container: str) -> str:
    return subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
        capture_output=True,
        text=True,
    ).stdout.strip()


def _wait_healthy(container: str, timeout: int = 120) -> None:
    deadline = time.monotonic() + timeout
    while _health(container) != "healthy":
        assert time.monotonic() < deadline, f"{container} is {_health(container)}"
        time.sleep(2)


def _compose(proj, *args):
    from vecten.stack.docker import SubprocessDocker

    files = [Path(f) for f in (proj / STACK_DIR / "compose.project").read_text().split()]
    profiles = ["platform", "mcp", "local"]
    return SubprocessDocker().compose(proj, files, profiles, list(args))


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
def test_up_runs_services_isolated_and_keeps_their_volume(tmp_path):
    build = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", "dev", "build-images", "--repo", REPO, "--json"],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    init = ("--name", "acme", "--users", "3", "--source", "hubspot", "--json")
    assert vecten("init", "p", *init, cwd=tmp_path).returncode == 0
    proj = tmp_path / "p"
    project = compose_project_name(proj, load_config(proj))
    drop_volumes(project)
    _extension(proj)
    added = vecten("extension", "add", "./extensions/acme-svc", "--json", cwd=proj)
    assert added.returncode == 0, added.stdout + added.stderr
    assert set(load_config(proj).services["acme-svc"]) == {"web", "crash"}
    try:
        up = vecten("up", "--json", cwd=proj)
        assert up.returncode == 0, up.stdout + up.stderr
        assert set(json.loads(up.stdout)["services"]) == {"acme-svc.web", "acme-svc.crash"}

        web = service_container(project, "svc-acme-svc-web")
        _wait_healthy(web)
        image = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", web],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert image.startswith(f"{project}-airflow:")

        # The service crash-loops while the stack keeps serving.
        with httpx.Client(verify=False, timeout=10) as client:
            assert front_door(client, "/health", "mcp.localhost:8443").status_code == 200
            airflow = front_door(client, "/api/v2/monitor/health", "airflow.localhost:8443")
            assert airflow.status_code == 200
        crash = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=svc-acme-svc-crash",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        deadline = time.monotonic() + 90
        while True:
            restarts = subprocess.run(
                ["docker", "inspect", "--format", "{{.RestartCount}}", crash],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if int(restarts or 0) >= 2:
                break
            assert time.monotonic() < deadline, f"crash restarted {restarts} times"
            time.sleep(3)

        # Airflow's network does not hold the service's name; NGINX reaches it on its own.
        scheduler = service_container(project, "airflow-scheduler")
        lookup = subprocess.run(
            [
                "docker",
                "exec",
                scheduler,
                "python",
                "-c",
                "import socket; socket.gethostbyname('svc-acme-svc-web')",
            ],
            capture_output=True,
            text=True,
        )
        assert lookup.returncode != 0 and "gaierror" in lookup.stderr
        nginx = service_container(project, "nginx")
        reached = subprocess.run(
            ["docker", "exec", nginx, "wget", "-qO-", "http://svc-acme-svc-web:8000/count"],
            capture_output=True,
            text=True,
        )
        assert reached.stdout.strip() == "1", reached.stderr

        # A recreated container mounts the same volume, written by the image's own user.
        recreate = _compose(proj, "up", "-d", "--no-build", "--force-recreate", "svc-acme-svc-web")
        assert recreate.returncode == 0, recreate.stderr
        web = service_container(project, "svc-acme-svc-web")
        _wait_healthy(web)
        again = subprocess.run(
            ["docker", "exec", nginx, "wget", "-qO-", "http://svc-acme-svc-web:8000/count"],
            capture_output=True,
            text=True,
        )
        assert again.stdout.strip() == "2", again.stderr
    finally:
        vecten("down", "--volumes", "--json", cwd=proj)
        # `extension add` installs the extension into the Python running the CLI.
        subprocess.run(["uv", "pip", "uninstall", "--python", sys.executable, "acme-svc"])
