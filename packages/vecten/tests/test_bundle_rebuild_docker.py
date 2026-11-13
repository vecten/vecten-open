"""A VM builds each release from what that release's bundle holds: a build-context file whose
content changed between two releases reaches the second build even when its size did not."""

import io
import secrets
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from vecten.deploy.bundle import build_host_bundle
from vecten_shared.config import load_config, save_config
from vecten_shared.provider.environment import EnvironmentOutputs

from .deploy_fixtures import COMMIT, STANDARD_OUTPUTS, render_aws
from .docker_util import docker_available

pytestmark = pytest.mark.docker
# Lineage and feedback come from git and the MCP; this test needs files alone.
METADATA = (
    {"commit": COMMIT, "ancestors": []},
    {"commit": COMMIT, "ancestors": [], "resolutions": [], "unreviewed": []},
)


def _release(root: Path, extension: str, out: Path) -> Path:
    """Bundle `root` with `extension` registered, then extract it as the VM agent does."""
    config = load_config(root)
    config.extensions = [extension]
    save_config(root, config)
    outputs = EnvironmentOutputs.model_validate(STANDARD_OUTPUTS)
    bundle = build_host_bundle(
        root, load_config(root), "prod", outputs, "mcp", COMMIT, out, metadata=METADATA
    )
    release = out / "release"
    release.mkdir()
    # The agent's own extraction: GNU tar keeps each member's modification time.
    subprocess.run(["tar", "-xzf", str(bundle), "-C", str(release), "--no-same-owner"], check=True)
    return release


def _built_requirements(release: Path, project: str) -> str:
    """Build an image from the release's build context and read back its requirements."""
    compose = release / "probe.yml"
    context = release / ".vecten" / "stack" / "build"
    service = {
        "build": {
            "context": str(context),
            "dockerfile_inline": "FROM scratch\nCOPY requirements.txt /requirements.txt\n",
        },
        "image": project,
    }
    compose.write_text(yaml.safe_dump({"services": {"probe": service}}))
    built = subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(compose), "build"],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    container = subprocess.run(
        ["docker", "create", project, "none"], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        # `docker cp` to standard output answers a tar stream.
        stream = subprocess.run(
            ["docker", "cp", f"{container}:/requirements.txt", "-"],
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(stream)) as tar:
            return tar.extractfile("requirements.txt").read().decode()
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


def test_a_changed_file_of_the_same_size_reaches_the_next_releases_build(tmp_path):
    if not docker_available():
        pytest.skip("Docker is not available")
    root = render_aws(tmp_path / "proj")
    # A name no earlier build has seen, so nothing BuildKit holds can stand in for it.
    name = f"acme-{secrets.token_hex(6)}"
    first = _release(root, f"{name}==1.0", tmp_path / "first")
    second = _release(root, f"{name}==2.0", tmp_path / "second")
    before = (first / ".vecten/stack/build/requirements.txt").read_text()
    after = (second / ".vecten/stack/build/requirements.txt").read_text()
    assert len(before) == len(after) and before != after
    project = f"vecten-fix-bundle-mtime-{secrets.token_hex(4)}"
    try:
        assert _built_requirements(first, f"{project}-a") == before
        assert _built_requirements(second, f"{project}-b") == after
    finally:
        for suffix in ("a", "b"):
            subprocess.run(
                ["docker", "image", "rm", "-f", f"{project}-{suffix}"], capture_output=True
            )
