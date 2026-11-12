"""Under systemd, Docker starts only once the agent's guard has added the services rule.

An Ubuntu 24.04 container booted with systemd as its init stands in for the VM; a oneshot
`docker.service` stands in for the daemon, since only the ordering between the two units is
under test. The guard unit and the agent copy are the agent's own.
"""

import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from vecten.deploy.vm import apply as agent

pytestmark = pytest.mark.docker

DOCKERFILE = """\
FROM ubuntu:24.04
RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
    systemd systemd-sysv python3 iptables \\
 && rm -rf /var/lib/apt/lists/*
CMD ["/sbin/init"]
"""
IMAGE = f"vecten-test-systemd:{hashlib.sha256(DOCKERFILE.encode()).hexdigest()[:12]}"
STAND_IN_DOCKER = """\
[Unit]
Description=Stand-in for the Docker daemon

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/true
"""
FAILING_IPTABLES = "#!/bin/sh\nexit 4\n"
BOOT_SECONDS = 60
RULE = ["DOCKER-USER", "-i", "vecten-services", "-d", "169.254.169.254", "-j", "DROP"]


def docker(*args, input=None, check=True):
    return subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, check=check
    )


def write(vm: str, path: str, text: str, mode: str = "0644") -> None:
    docker("exec", "-i", vm, "sh", "-c", f"mkdir -p $(dirname {path}) && cat > {path}", input=text)
    docker("exec", vm, "chmod", mode, path)


def host_json() -> str:
    doc = {key: "" for key in agent.HOST_KEYS}
    doc.update(
        {
            "host": "platform",
            "cloud": "aws",
            "components": ["platform"],
            "env": "prod",
            "connector_secrets": [],
            "service_secrets": [],
            "service_network": "vecten-services",
        }
    )
    return json.dumps(doc)


@pytest.fixture(scope="module")
def image():
    if docker("info", check=False).returncode != 0:
        pytest.skip("no Docker daemon")
    if docker("image", "inspect", IMAGE, check=False).returncode != 0:
        docker("build", "-t", IMAGE, "-", input=DOCKERFILE)
    return IMAGE


@pytest.fixture
def vm(image):
    name = f"vecten-guard-{uuid.uuid4().hex[:8]}"
    docker(
        "run",
        "-d",
        "--privileged",
        "--cgroupns=host",
        "-v",
        "/sys/fs/cgroup:/sys/fs/cgroup:rw",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/run/lock",
        "--name",
        name,
        image,
    )
    try:
        deadline = time.monotonic() + BOOT_SECONDS
        while time.monotonic() < deadline:
            state = docker("exec", name, "systemctl", "is-system-running", check=False)
            if state.stdout.strip() in ("running", "degraded"):
                break
            time.sleep(1)
        else:
            pytest.fail(f"systemd in {name} did not finish booting")
        write(name, "/etc/systemd/system/docker.service", STAND_IN_DOCKER)
        write(name, str(agent.GUARD_AGENT), Path(agent.__file__).read_text())
        write(name, f"/etc/systemd/system/{agent.GUARD_UNIT}", agent.GUARD_UNIT_TEXT)
        write(name, f"{agent.RELEASES}/c1/{agent.SHIP_DIR}/host.json", host_json())
        docker("exec", name, "ln", "-s", f"{agent.RELEASES}/c1", f"{agent.RELEASES}/current")
        docker("exec", name, "systemctl", "daemon-reload")
        docker("exec", name, "systemctl", "enable", agent.GUARD_UNIT)
        yield name
    finally:
        docker("rm", "-f", "-v", name, check=False)


def active(vm: str, unit: str) -> str:
    return docker("exec", vm, "systemctl", "is-active", unit, check=False).stdout.strip()


def start_docker(vm: str) -> subprocess.CompletedProcess:
    return docker("exec", vm, "systemctl", "start", "docker.service", check=False)


def test_a_failed_guard_keeps_docker_down(vm):
    # The guard runs whatever iptables comes first on systemd's PATH.
    write(vm, "/usr/local/sbin/iptables", FAILING_IPTABLES, "0755")
    assert start_docker(vm).returncode != 0
    assert active(vm, "docker.service") != "active"
    assert active(vm, agent.GUARD_UNIT) == "failed"


def test_docker_starts_after_the_guard_added_the_rule(vm):
    assert start_docker(vm).returncode == 0
    assert active(vm, "docker.service") == "active"
    assert active(vm, agent.GUARD_UNIT) == "active"
    check = docker("exec", vm, "iptables", "-w", "30", "-C", *RULE, check=False)
    assert check.returncode == 0, check.stderr


def test_docker_starts_where_no_release_was_shipped(vm):
    docker("exec", vm, "rm", f"{agent.RELEASES}/current")
    write(vm, "/usr/local/sbin/iptables", FAILING_IPTABLES, "0755")
    assert start_docker(vm).returncode == 0
    assert active(vm, "docker.service") == "active"
