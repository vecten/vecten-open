"""The MCP test image is built from the current checkout once per test run, never reused from
an earlier one."""

import subprocess
from pathlib import Path

from . import docker_util


def test_a_cached_image_is_rebuilt_once_per_run_and_reused_within_it(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[:2])
        if argv[:2] == ["docker", "build"]:
            # The Dockerfile installs the dependencies from the lock, so the context needs it.
            context = Path(argv[-1])
            for name in docker_util.LOCK_INPUTS:
                assert (context / name).read_bytes() == (docker_util.REPO / name).read_bytes()
        # Every image already exists locally, as after any earlier run.
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(docker_util, "docker_available", lambda: True)
    monkeypatch.setattr(docker_util.subprocess, "run", run)
    monkeypatch.setattr(docker_util, "_built_mcp_image", None)
    first = docker_util.ensure_mcp_image()
    assert ["docker", "build"] in calls
    assert calls.count(["uv", "build"]) == 2
    calls.clear()
    assert docker_util.ensure_mcp_image() == first
    assert calls == []
