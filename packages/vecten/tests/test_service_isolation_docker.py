"""The VM agent's own rule keeps a container on the services bridge off the metadata address.

A Docker daemon in a privileged container stands in for the VM. The fake metadata listener
sits in a network namespace of its own behind a veth, as the real one sits off the host: a
listener on the host itself is reached through INPUT, which DOCKER-USER never sees. A shell
stays the container's first process, so dockerd can restart inside one network namespace,
as it does on a VM when its package is upgraded.
"""

import subprocess
import time
import uuid

import pytest

from vecten.deploy.vm import apply as agent
from vecten.stack.compose import SERVICES_BRIDGE
from vecten_shared.versions import VERSIONS

pytestmark = pytest.mark.docker

# The engine lines the VMs install: Amazon Linux 2023's docker package and Ubuntu 24.04's.
ENGINES = ("docker:25.0.5-dind", "docker:28.5.2-dind")
PROBE = VERSIONS.log_cleaner_image
METADATA = agent.METADATA_ADDRESSES["aws"]
DAEMON_SECONDS = 60
SUPERVISED_DOCKERD = (
    "while true; do dockerd-entrypoint.sh dockerd >>/var/log/dockerd.log 2>&1; sleep 1; done"
)
# Busybox ignores `peer name`, so the peer is renamed once created.
METADATA_LISTENER = f"""
set -e
unshare -n sleep 100000 >/dev/null 2>&1 &
P=$!
sleep 1
ip link add vmeta0 type veth
PEER=$(ip -o link show vmeta0 | sed -n 's/.*vmeta0@\\([^:]*\\):.*/\\1/p')
ip link set "$PEER" name vmeta1
ip link set vmeta1 netns "$P"
ip addr add 169.254.0.1/30 dev vmeta0
ip link set vmeta0 up
nsenter -t "$P" -n sh -c 'ip addr add 169.254.0.2/30 dev vmeta1; \
ip addr add {METADATA}/32 dev vmeta1; ip link set vmeta1 up; ip link set lo up; \
ip route add default via 169.254.0.1'
ip route add {METADATA}/32 via 169.254.0.2
nsenter -t "$P" -n sh -c 'while true; do \
printf "HTTP/1.0 200 OK\\r\\n\\r\\ntoken\\n" | nc -l -p 80 >/dev/null; done' >/dev/null 2>&1 &
"""


def docker(*args, input=None, check=True):
    return subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, check=check
    )


def ensure_image(ref: str) -> None:
    if docker("image", "inspect", ref, check=False).returncode == 0:
        return
    for attempt in range(3):
        if docker("pull", "-q", ref, check=False).returncode == 0:
            return
        time.sleep(5 * (attempt + 1))
    pytest.fail(f"cannot pull {ref}")


def wait_for_daemon(vm: str, other_than: str = "") -> None:
    deadline = time.monotonic() + DAEMON_SECONDS
    while time.monotonic() < deadline:
        pid = docker("exec", vm, "pidof", "dockerd", check=False).stdout.strip()
        if (
            pid
            and pid != other_than
            and docker("exec", vm, "docker", "info", check=False).returncode == 0
        ):
            return
        time.sleep(1)
    log = docker("exec", vm, "tail", "-20", "/var/log/dockerd.log", check=False).stdout
    pytest.fail(f"dockerd in {vm} did not answer within {DAEMON_SECONDS} s:\n{log}")


@pytest.fixture(params=ENGINES)
def vm(request):
    if docker("info", check=False).returncode != 0:
        pytest.skip("no Docker daemon")
    engine = request.param
    ensure_image(engine)
    ensure_image(PROBE)
    name = f"vecten-isolation-{uuid.uuid4().hex[:8]}"
    docker(
        "run",
        "-d",
        "--privileged",
        "--name",
        name,
        "--entrypoint",
        "sh",
        engine,
        "-c",
        SUPERVISED_DOCKERD,
    )
    try:
        wait_for_daemon(name)
        saved = subprocess.run(["docker", "save", PROBE], capture_output=True, check=True).stdout
        subprocess.run(
            ["docker", "exec", "-i", name, "docker", "load", "-q"],
            input=saved,
            capture_output=True,
            check=True,
        )
        docker("exec", "-i", name, "sh", input=METADATA_LISTENER)
        docker(
            "exec",
            name,
            "docker",
            "network",
            "create",
            "-o",
            f"com.docker.network.bridge.name={SERVICES_BRIDGE}",
            "vecten-services",
        )
        yield name
    finally:
        docker("rm", "-f", "-v", name, check=False)


def in_vm(vm: str, calls: list):
    """The agent's `run`, executing each command on the stand-in VM."""

    def run(args, env=None, input=None):
        calls.append(list(args))
        return subprocess.run(
            ["docker", "exec", vm, *args], input=input, capture_output=True, text=True, check=False
        )

    return run


def fetch(vm: str, container: str) -> subprocess.CompletedProcess:
    return docker(
        "exec",
        vm,
        "docker",
        "exec",
        container,
        "wget",
        "-T",
        "3",
        "-qO-",
        f"http://{METADATA}/",
        check=False,
    )


def reads_metadata(vm: str, container: str) -> bool:
    return fetch(vm, container).stdout.strip() == "token"


def dropped(vm: str, container: str) -> bool:
    result = fetch(vm, container)
    # A DROP times out; a refusal or an unreachable route would mean a broken stand-in.
    return result.returncode != 0 and "timed out" in result.stdout + result.stderr


PROJECT = "vecten-acme-prod"


def start(vm: str, name: str, service: str, *network: str) -> None:
    """A container labelled as Compose labels the stack's, restarted with the daemon."""
    docker(
        "exec",
        vm,
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"com.docker.compose.project={PROJECT}",
        "--label",
        f"com.docker.compose.service={service}",
        "--restart",
        "unless-stopped",
        *network,
        PROBE,
        "sleep",
        "1d",
    )


def running(vm: str, name: str) -> None:
    deadline = time.monotonic() + DAEMON_SECONDS
    while time.monotonic() < deadline:
        state = docker(
            "exec", vm, "docker", "inspect", "-f", "{{.State.Running}}", name, check=False
        )
        if state.stdout.strip() == "true":
            return
        time.sleep(1)
    pytest.fail(f"{name} did not come back after dockerd restarted")


def test_the_agents_rule_drops_the_services_bridge_and_survives_a_dockerd_restart(vm):
    start(vm, "svc", "svc-acme-bot", "--network", "vecten-services")
    start(vm, "dag", "airflow-scheduler")
    # Without the rule a service reaches the metadata address like any container.
    assert reads_metadata(vm, "svc") and reads_metadata(vm, "dag")

    calls = []
    host = {"cloud": "aws", "service_network": SERVICES_BRIDGE}
    agent.isolate_services(host, in_vm(vm, calls))
    assert [c[3] for c in calls] == ["-C", "-I"]
    assert dropped(vm, "svc")
    assert reads_metadata(vm, "dag")

    pid = docker("exec", vm, "pidof", "dockerd").stdout.strip()
    docker("exec", vm, "kill", pid)
    wait_for_daemon(vm, other_than=pid)
    running(vm, "svc")
    running(vm, "dag")
    assert dropped(vm, "svc")
    assert reads_metadata(vm, "dag")
    # The rule is still in place, so the next ship or boot inserts no second one.
    calls.clear()
    agent.isolate_services(host, in_vm(vm, calls))
    assert [c[3] for c in calls] == ["-C"]


def state(vm: str, name: str) -> str:
    return docker("exec", vm, "docker", "inspect", "-f", "{{.State.Running}}", name).stdout.strip()


def test_services_the_rule_cannot_cover_are_stopped_and_the_stack_kept(vm):
    start(vm, "svc", "svc-acme-bot", "--network", "vecten-services")
    start(vm, "dag", "airflow-scheduler")
    agent.stop_services({"compose_project": PROJECT}, in_vm(vm, []))
    assert state(vm, "svc") == "false"
    assert state(vm, "dag") == "true"
