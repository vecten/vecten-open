"""The generated local stack starts the configured MCP process, not just its base image."""

import os
import subprocess
import uuid

import pytest
import yaml

from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten.stack.compose import write_compose
from vecten.stack.env import ensure_local_env
from vecten_shared.config import load_config

from .docker_util import ensure_mcp_image

pytestmark = pytest.mark.docker


def test_generated_local_mcp_stack_is_healthy(tmp_path):
    image = os.environ.get("VECTEN_TEST_MCP_IMAGE") or ensure_mcp_image()
    root = tmp_path / "project"
    render_project(root, ScaffoldParams(project_name="smoke", users=3, sources=["demo"]))
    config = load_config(root)
    secrets = ensure_local_env(root, config)
    path = write_compose(root, config)
    doc = yaml.safe_load(path.read_text())
    doc["services"] = {name: doc["services"][name] for name in ("postgres", "mcp-server")}
    doc["services"]["postgres"].pop("ports")
    mcp = doc["services"]["mcp-server"]
    mcp.pop("build")
    mcp["image"] = image
    path.write_text(yaml.safe_dump(doc))
    command = [
        "docker",
        "compose",
        "--project-name",
        "vecten-mcp-test-" + uuid.uuid4().hex[:10],
        "--env-file",
        str(root / ".env"),
        "-f",
        str(path),
        "--profile",
        "local",
        "--profile",
        "mcp",
    ]
    try:
        result = subprocess.run(
            [*command, "up", "-d", "--wait", "--wait-timeout", "45"],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            logs = subprocess.run(
                [*command, "logs", "--no-color", "mcp-server"],
                capture_output=True,
                text=True,
            ).stdout
            for value in secrets.values():
                if value:
                    logs = logs.replace(value, "<redacted>")
            pytest.fail("Generated MCP stack failed startup:\n" + logs[-5000:])
    finally:
        subprocess.run([*command, "down", "--volumes"], capture_output=True, check=True)
