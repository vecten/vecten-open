"""Both images build an in-repo extension from extensions/ and hold only the install."""

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from vecten import extensions, in_repo, locking
from vecten.stack.compose import _build
from vecten.stack.images import write_project_layer
from vecten_shared.config import VectenConfig, save_config
from vecten_shared.extension_record import Extension
from vecten_shared.versions import VERSIONS

from .docker_util import require_base_image

pytestmark = pytest.mark.docker
FIXTURE = Path(__file__).parent / "fixtures" / "extension"
NAME = "vecten-test-extension"


def _base_pin(kind: str, name: str) -> str:
    """The base image's own pin of `name`, as a lock resolved against it holds."""
    pins = extensions.base_constraints(kind).splitlines()
    return next(line for line in pins if line.lower().startswith(f"{name}=="))


def _project(root: Path, kind: str) -> VectenConfig:
    shutil.copytree(FIXTURE, root / "extensions" / NAME)
    config = VectenConfig.model_validate(
        {
            "project": {"name": "in-repo-test", "scaffold_version": VERSIONS.vecten},
            "users": 2,
            "extensions": [NAME],
        }
    )
    save_config(root, config)
    info = in_repo.check(root, f"./extensions/{NAME}")
    build = extensions.resolve_lock(list(info.build_requires), root, runtime=True, cwd=root)
    record = {
        NAME: Extension(
            info.version,
            info.extras,
            source_path=info.path,
            metadata_sha256=info.metadata_sha256,
            build_requires=tuple(line for line in build.splitlines() if "==" in line),
        )
    }
    (root / locking.PINS_FILE).write_text(f"./extensions/{NAME}\n")
    (root / locking.RECORD_FILE).write_text(locking.record_text(record))
    for component in locking.components(config):
        roots = locking.roots(config, {}, record, component)
        digest = locking.inputs_hash(component, [*roots, *locking.metadata_markers(record)])
        # The fixture's one dependency, as `vecten lock` would pin it against the base image.
        (root / locking.LOCK_FILES[component]).write_text(
            locking.stamp_line(component, digest) + f"\n{_base_pin(kind, 'packaging')}\n"
        )
    return config


@pytest.mark.parametrize("kind", ["airflow", "mcp"])
def test_the_image_installs_the_in_repo_extension_and_not_its_source(tmp_path, kind):
    require_base_image(kind)
    root = tmp_path / "project"
    root.mkdir()
    config = _project(root, kind)
    stack = root / ".vecten" / "stack"
    write_project_layer(stack, config, None)
    image = f"vecten-in-repo-test-{kind}-{uuid.uuid4().hex[:8]}"
    compose = {
        "services": {"probe": {"build": _build("vm", stack, f"{kind}.Dockerfile"), "image": image}}
    }
    (stack / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    try:
        build = subprocess.run(
            ["docker", "compose", "-p", image, "build"],
            cwd=stack,
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        probe = (
            "import importlib.metadata as m, pathlib, vecten_test_extension as e; "
            f"assert m.version({NAME!r}) == '1.0'; "
            "groups = {p.group for p in m.distribution('vecten-test-extension').entry_points}; "
            "assert 'vecten.dags' in groups and 'vecten.mcp.tools' in groups, groups; "
            "assert 'site-packages' in e.__file__, e.__file__; "
            "assert not pathlib.Path('/tmp/extensions').exists()"
        )
        run = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image]
            + ["-c", probe],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert (root / "extensions" / NAME / "pyproject.toml").is_file()
        assert not list((root / "extensions" / NAME).glob("*.egg-info"))
    finally:
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)
