"""The agent names, against a real Docker, the service volumes a release no longer declares."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

import vecten.deploy.vm
from vecten_shared.versions import VERSIONS

pytestmark = pytest.mark.docker
APPLY = Path(vecten.deploy.vm.__file__).parent / "apply.py"


def docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def load_agent():
    spec = importlib.util.spec_from_file_location("vecten_vm_apply_volumes", APPLY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_file(release: Path, volumes: list[str]) -> None:
    stack = release / ".vecten" / "stack"
    stack.mkdir(parents=True, exist_ok=True)
    mounts = "".join(f"      - {v}:/data/{v}\n" for v in volumes)
    declared = "".join(f"  {v}: {{}}\n" for v in volumes)
    (stack / "docker-compose.yml").write_text(
        "services:\n"
        "  holder:\n"
        f"    image: {VERSIONS.nginx_image}\n"
        "    profiles: [platform]\n"
        + (f"    volumes:\n{mounts}" if volumes else "")
        + (f"volumes:\n{declared}" if volumes else "")
    )


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
def test_a_service_volume_the_release_dropped_is_named_and_kept(tmp_path):
    agent = load_agent()
    project = f"vecten-vol-{uuid.uuid4().hex[:8]}"
    host = {"compose_project": project, "components": ["platform"]}
    release = tmp_path / "release"
    compose_file(release, ["svc-acme-bot-data", "svc-acme-old-data", "pgdata"])
    base = agent._compose(release, host)
    try:
        subprocess.run([*base, "up", "-d"], check=True, capture_output=True)
        compose_file(release, ["svc-acme-bot-data"])
        subprocess.run([*base, "up", "-d", "--remove-orphans"], check=True, capture_output=True)
        found = agent.orphaned_volumes(release, host, {}, agent.run)
        assert found == [f"{project}_svc-acme-old-data"]
        listed = subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", f"name={project}_svc-acme-old-data"],
            capture_output=True,
            text=True,
        ).stdout
        assert listed.strip() == f"{project}_svc-acme-old-data"
    finally:
        subprocess.run([*base, "down", "--volumes"], capture_output=True)
        for name in ("svc-acme-bot-data", "svc-acme-old-data", "pgdata"):
            subprocess.run(
                ["docker", "volume", "rm", "-f", f"{project}_{name}"], capture_output=True
            )
