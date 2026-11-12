"""Both images build a git-sourced extension from a ship bundle: CI fetched the recorded commit
from a git host that needs a token, and the host builds the bundled source with no git, no
network to that host and no token, after checking its source_sha256."""

from __future__ import annotations

import gzip
import os
import subprocess
import tarfile
import tomllib
import uuid
from pathlib import Path

import pytest
import yaml

from vecten import extensions
from vecten.stack.images import base_image
from vecten.stack.up import PUBLISHED_REGISTRY
from vecten_shared.config import load_config
from vecten_shared.provider.environment import EnvironmentOutputs

from . import test_git_ship
from .deploy_fixtures import COMMIT, STANDARD_OUTPUTS, render_aws
from .docker_util import docker_available
from .test_git_extensions import PYPROJECT
from .test_git_ship import NAME, TAG, git_project, served_origin

pytestmark = pytest.mark.docker
# The local git host and its token, as the unit tests of git sources serve them.
remote, served = test_git_ship.remote, test_git_ship.served
# The base images `vecten dev build-images` tags, under the registry `vecten lock` also reads.
REGISTRY = os.environ.get("VECTEN_IMAGE_REGISTRY") or None
HOSTS = {"airflow": "platform", "mcp": "mcp"}


def _require_base_image(kind: str) -> str:
    if not docker_available():
        pytest.skip("Docker is not available")
    image = base_image(kind, REGISTRY)
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        message = f"{image} is missing; build it with vecten dev build-images"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return image


def _base_pin(kind: str, name: str) -> str:
    pins = extensions.base_constraints(kind).splitlines()
    return next(line for line in pins if line.lower().startswith(f"{name}=="))


def _project(tmp_path: Path, served, kind: str) -> Path:
    """A deploy project with the served extension recorded at its tag's commit, its build
    backend pinned as `vecten lock` pins it, and its one dependency pinned as the base has it."""
    root = render_aws(tmp_path / "proj")
    config = load_config(root)
    config.extensions = [NAME]
    text = PYPROJECT.format(name=NAME, version='version = "1.2.0"')
    requires = tomllib.loads(text)["build-system"]["requires"]
    build = extensions.resolve_lock(requires, root, runtime=True, cwd=root)
    git_project(
        root,
        config,
        served_origin(served),
        served.commits.hash_of(TAG, served.tmp),
        build=tuple(line for line in build.splitlines() if "==" in line),
        body=f"{_base_pin(kind, 'httpx')}\n",
    )
    return root


def _ship_bundle(root: Path, out: Path, host: str) -> Path:
    from vecten.deploy.bundle import build_host_bundle

    outputs = EnvironmentOutputs.model_validate(STANDARD_OUTPUTS)
    bundle = build_host_bundle(root, load_config(root), "prod", outputs, host, COMMIT, out)
    release = out / "release"
    release.mkdir()
    # The agent extracts with GNU tar, which keeps each member's mode.
    with tarfile.open(bundle, "r:gz") as tar:
        tar.extractall(release, filter="tar")
    return release


def _build(release: Path, kind: str, image: str) -> subprocess.CompletedProcess:
    stack = release / ".vecten" / "stack"
    dockerfile = stack / "build" / f"{kind}.Dockerfile"
    text = dockerfile.read_text()
    # The bundle names the published base image; this test builds on the one from this checkout.
    dockerfile.write_text(
        text.replace(base_image(kind, PUBLISHED_REGISTRY), base_image(kind, REGISTRY))
    )
    shipped = yaml.safe_load((stack / "docker-compose.yml").read_text())
    build = next(s["build"] for s in shipped["services"].values() if "build" in s)
    build["dockerfile"] = f"{kind}.Dockerfile"
    probe = {"services": {"probe": {"build": build, "image": image}}}
    (stack / "probe.yml").write_text(yaml.safe_dump(probe))
    return subprocess.run(
        ["docker", "compose", "-p", image, "-f", "probe.yml", "build", "--progress", "plain"],
        cwd=stack,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("kind", ["airflow", "mcp"])
def test_the_image_builds_the_bundled_git_source_without_the_token(served, tmp_path, kind):
    _require_base_image(kind)
    root = _project(tmp_path, served, kind)
    release = _ship_bundle(root, tmp_path / "out", HOSTS[kind])
    bundle = next((tmp_path / "out").glob("*.tar.gz"))
    assert served.token.encode() not in gzip.decompress(bundle.read_bytes())
    image = f"vecten-git-ship-test-{kind}-{uuid.uuid4().hex[:8]}"
    try:
        built = _build(release, kind, image)
        assert built.returncode == 0, built.stdout + built.stderr
        assert f"verified {NAME} at {served.commits.commits[TAG]}" in built.stdout + built.stderr
        probe = (
            "import importlib.metadata as m, pathlib, acme_resolution as e; "
            f"assert m.version({NAME!r}) == '1.2.0'; "
            "assert 'site-packages' in e.__file__, e.__file__; "
            "assert not pathlib.Path('/tmp/sources').exists(); "
            "assert not pathlib.Path('/tmp/extensions.lock.json').exists(); "
            "files = [pathlib.Path(f.locate()) for f in m.files('acme-resolution')]; "
            f"assert not any({served.token!r} in p.read_text(errors='replace') "
            "for p in files if p.is_file())"
        )
        run = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image]
            + ["-c", probe],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        history = subprocess.run(
            ["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image],
            capture_output=True,
            text=True,
        )
        assert served.token not in history.stdout
        assert "COPY" not in "".join(
            line for line in history.stdout.splitlines() if "sources" in line
        )
    finally:
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)


def test_a_tampered_bundled_source_fails_the_build_naming_both_hashes(served, tmp_path):
    from vecten_shared.git_sources import source_sha256

    kind = "mcp"
    _require_base_image(kind)
    root = _project(tmp_path, served, kind)
    release = _ship_bundle(root, tmp_path / "out", HOSTS[kind])
    source = release / "extensions" / NAME
    (source / "src" / "acme_resolution" / "__init__.py").write_text("dags = ['injected']\n")
    tampered = source_sha256(source)
    recorded = served.commits.hash_of(TAG, served.tmp)
    image = f"vecten-git-ship-tampered-{uuid.uuid4().hex[:8]}"
    try:
        built = _build(release, kind, image)
        output = built.stdout + built.stderr
        assert built.returncode != 0, output
        assert NAME in output and served.commits.commits[TAG] in output
        assert tampered in output and recorded in output
    finally:
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)
