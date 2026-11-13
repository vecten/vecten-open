"""A Streamlit app from an in-repo extension runs as its generated service in the platform image.

The stack's own services stay down: only the project's Airflow image is built and the app's
service started from the Compose file `vecten up` writes, so the test publishes no host port
and runs beside other stacks on the same daemon.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vecten.stack.compose import STACK_DIR, compose_project_name
from vecten_shared.config import load_config

from .docker_util import remove_stack_images
from .test_up_e2e import REPO, docker_ok, drop_volumes, service_container, vecten

pytestmark = pytest.mark.docker

PYPROJECT = """\
[project]
name = "acme-dash"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = []
[project.optional-dependencies]
platform = ["streamlit"]
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/acme_dash"]
[project.entry-points."vecten.apps"]
overview = "acme_dash:overview"
"""
PACKAGE = """\
overview = {
    "title": "Overview",
    "description": "A fixture dashboard",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme_dash/app.py",
    "datasets": ["orders"],
    "groups": ["default"],
}
"""
APP = 'import streamlit as st\n\nst.title("acme overview")\n'
SERVICE = "svc-acme-dash-app-overview"
# Opens the app's WebSocket as its browser page does and prints whether that page would send
# usage statistics: Streamlit's page reads the flag from the session's first message.
PROBE = """\
import asyncio, sys
from tornado.websocket import websocket_connect
from streamlit.proto.BackMsg_pb2 import BackMsg
from streamlit.proto.ForwardMsg_pb2 import ForwardMsg


async def main(port):
    conn = await websocket_connect(
        f"ws://localhost:{port}/_stcore/stream", subprotocols=["streamlit"]
    )
    back = BackMsg()
    back.rerun_script.query_string = ""
    await conn.write_message(back.SerializeToString(), binary=True)
    while True:
        data = await asyncio.wait_for(conn.read_message(), 60)
        if data is None:
            sys.exit("the WebSocket closed")
        message = ForwardMsg()
        message.ParseFromString(data)
        if message.WhichOneof("type") == "new_session":
            print(message.new_session.config.gather_usage_stats)
            return


asyncio.run(main(sys.argv[1]))
"""

PROCESSES = """\
import json, pathlib
found = []
for proc in pathlib.Path("/proc").iterdir():
    if proc.name.isdigit():
        try:
            raw = (proc / "cmdline").read_bytes()
        except OSError:
            continue
        if raw:
            # One NUL ends each argument, an empty last one included.
            found.append(raw[:-1].decode().split("\\0"))
print(json.dumps(found))
"""


def _extension(proj: Path) -> None:
    root = proj / "extensions" / "acme-dash"
    package = root / "src" / "acme_dash"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (package / "__init__.py").write_text(PACKAGE)
    (package / "app.py").write_text(APP)


def _compose(proj: Path, *args: str):
    from vecten.stack.docker import SubprocessDocker

    files = [Path(f) for f in (proj / STACK_DIR / "compose.project").read_text().split()]
    return SubprocessDocker().compose(proj, files, ["platform", "mcp", "local"], list(args))


def _health(container: str) -> str:
    return subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
        capture_output=True,
        text=True,
    ).stdout.strip()


def _probe(container: str, port: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-i", container, "python", "-", str(port)],
        input=PROBE,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
def test_a_streamlit_app_runs_from_its_package_with_usage_statistics_off(tmp_path):
    from vecten.stack import up as up_mod

    build = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", "dev", "build-images", "--repo", REPO, "--json"],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    init = ("--name", "apps", "--users", "3", "--source", "hubspot", "--json")
    assert vecten("init", "p", *init, cwd=tmp_path).returncode == 0
    proj = tmp_path / "p"
    project = compose_project_name(proj, load_config(proj))
    _extension(proj)
    added = vecten("extension", "add", "./extensions/acme-dash", "--json", cwd=proj)
    assert added.returncode == 0, added.stdout + added.stderr
    config = load_config(proj)
    assert config.apps["acme-dash"]["overview"].groups == ["default"]
    try:
        up_mod.ensure_local_env(proj, config)
        up_mod.write_compose(proj, config)
        up_mod.write_project_layer(proj / STACK_DIR, config, None)
        built = _compose(proj, "build", "airflow-api-server")
        assert built.returncode == 0, built.stdout + built.stderr
        started = _compose(proj, "up", "-d", "--no-build", "--no-deps", SERVICE)
        assert started.returncode == 0, started.stdout + started.stderr
        app = service_container(project, SERVICE)
        deadline = time.monotonic() + 180
        while _health(app) != "healthy":
            logs = subprocess.run(["docker", "logs", app], capture_output=True, text=True)
            assert time.monotonic() < deadline, f"{_health(app)}: {logs.stdout}{logs.stderr}"
            time.sleep(3)
        inspect = json.loads(
            subprocess.run(["docker", "inspect", app], capture_output=True, text=True).stdout
        )[0]
        assert inspect["Config"]["Image"].startswith(f"{project}-airflow:")
        assert inspect["Config"]["Cmd"] == [
            "python",
            "-m",
            "vecten_shared.launch",
            "streamlit",
            "acme_dash/app.py",
        ]
        # The launcher replaced itself with Streamlit, under the image's own init.
        listing = subprocess.run(
            ["docker", "exec", "-i", app, "python", "-"],
            input=PROCESSES,
            capture_output=True,
            text=True,
        )
        # dumb-init, the image's PID 1, keeps the command it was started with.
        commands = [c for c in json.loads(listing.stdout) if not c[0].endswith("dumb-init")]
        assert not [c for c in commands if "vecten_shared.launch" in c], commands
        [cmdline] = [c for c in commands if c[1:4] == ["-m", "streamlit", "run"]]
        assert cmdline[4].endswith("site-packages/acme_dash/app.py")
        assert cmdline[5:] == [
            "--server.port",
            "8501",
            "--server.address",
            "0.0.0.0",
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
            "--server.enableXsrfProtection",
            "true",
            "--server.baseUrlPath",
            "",
        ]

        probe = _probe(app, 8501)
        assert probe.returncode == 0, probe.stdout + probe.stderr
        assert probe.stdout.strip() == "False"

        # The same probe against Streamlit's own defaults sees statistics on, so the answer
        # above comes from the launcher's flag.
        subprocess.Popen(
            ["docker", "exec", "-d", app, "python", "-m", "streamlit", "run", cmdline[4]]
            + ["--server.port", "8502", "--server.headless", "true"],
        ).wait()
        deadline = time.monotonic() + 60
        while True:
            control = _probe(app, 8502)
            if control.returncode == 0:
                break
            assert time.monotonic() < deadline, control.stdout + control.stderr
            time.sleep(2)
        assert control.stdout.strip() == "True"
    finally:
        _compose(proj, "down", "--volumes", "--remove-orphans")
        drop_volumes(project)
        remove_stack_images(project)
        # `extension add` installs the extension into the Python running the CLI.
        subprocess.run(["uv", "pip", "uninstall", "--python", sys.executable, "acme-dash"])
