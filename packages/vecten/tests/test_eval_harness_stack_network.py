"""The harness warehouse joins the local stack's network, so the stack's containers reach it
at warehouse.acme.test."""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import stack_network  # noqa: E402

LISTING = (
    "vecten-acme-1a2b3c4d_default\tvecten-acme-1a2b3c4d\n"
    "other_default\tother\n"
    "vecten-x-00000000_default\tvecten-x-00000000\n"
)


def test_networks_to_join_picks_new_vecten_stack_networks():
    joined = {"vecten-x-00000000_default"}
    assert stack_network.networks_to_join(LISTING, joined) == ["vecten-acme-1a2b3c4d_default"]
    assert stack_network.networks_to_join("", set()) == []


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


@pytest.mark.docker
def test_join_once_makes_the_alias_resolve_on_the_stack_network():
    tag = uuid.uuid4().hex[:8]
    project = f"vecten-evaltest-{tag}"
    network = f"{project}_default"
    warehouse, peer = f"evaltest-warehouse-{tag}", f"evaltest-peer-{tag}"
    try:
        docker(
            "network",
            "create",
            "--label",
            "com.docker.compose.network=default",
            "--label",
            f"com.docker.compose.project={project}",
            network,
        )
        docker("run", "-d", "--name", warehouse, "alpine:3.24", "sleep", "120")
        docker("run", "-d", "--name", peer, "--network", network, "alpine:3.24", "sleep", "120")
        assert network in stack_network.join_once(warehouse, "warehouse.acme.test")
        assert network in stack_network.joined(warehouse)
        assert network not in stack_network.join_once(warehouse, "warehouse.acme.test")
        found = docker("exec", peer, "getent", "hosts", "warehouse.acme.test", check=False)
        assert found.returncode == 0, found.stderr
    finally:
        docker("rm", "-f", warehouse, peer, check=False)
        docker("network", "rm", network, check=False)
