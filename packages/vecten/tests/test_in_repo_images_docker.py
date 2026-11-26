"""Both images build an in-repo extension from extensions/ and hold only the install."""

import json
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
OLD_HATCHLING = "vecten-old-hatchling"
SETUPTOOLS = "vecten-setuptools-extension"
SKILL = "monthly-orders"


def _base_pin(kind: str, name: str) -> str:
    """The base image's own pin of `name`, as a lock resolved against it holds."""
    pins = extensions.base_constraints(kind).splitlines()
    return next(line for line in pins if line.lower().startswith(f"{name}=="))


def _config(names: list[str], **fields: object) -> VectenConfig:
    return VectenConfig.model_validate(
        {
            "project": {"name": "in-repo-test", "scaffold_version": VERSIONS.vecten},
            "users": 2,
            "extensions": names,
            **fields,
        }
    )


def _record(root: Path, config: VectenConfig, kind: str, names: list[str]) -> None:
    """Lock the in-repo extensions `names` as `vecten lock` does: each one's build backend
    pinned on its own, and the fixtures' one dependency pinned as the base image has it."""
    save_config(root, config)
    record = {}
    for name in names:
        info = in_repo.check(root, f"./extensions/{name}")
        build = extensions.resolve_lock(list(info.build_requires), root, runtime=True, cwd=root)
        record[name] = Extension(
            info.version,
            info.extras,
            source_path=info.path,
            metadata_sha256=info.metadata_sha256,
            build_requires=tuple(line for line in build.splitlines() if "==" in line),
        )
    (root / locking.PINS_FILE).write_text("".join(f"./extensions/{n}\n" for n in sorted(names)))
    (root / locking.RECORD_FILE).write_text(locking.record_text(record))
    for component in locking.components(config):
        roots = locking.roots(config, {}, record, component)
        digest = locking.inputs_hash(component, [*roots, *locking.metadata_markers(record)])
        (root / locking.LOCK_FILES[component]).write_text(
            locking.stamp_line(component, digest) + f"\n{_base_pin(kind, 'packaging')}\n"
        )


def _project(root: Path, kind: str) -> VectenConfig:
    shutil.copytree(FIXTURE, root / "extensions" / NAME)
    config = _config([NAME])
    _record(root, config, kind, [NAME])
    return config


def _tiny(root: Path, name: str, requires: list[str], backend: str) -> None:
    """A src-layout package with no dependencies, built by `backend`."""
    package = root / "extensions" / name / "src" / name.replace("-", "_")
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package.parent.parent / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "1.0"\nrequires-python = ">=3.14"\n'
        f"dependencies = []\n[build-system]\nrequires = {json.dumps(requires)}\n"
        f'build-backend = "{backend}"\n'
    )


def _build_image(stack: Path, kind: str, image: str) -> subprocess.CompletedProcess:
    compose = {
        "services": {"probe": {"build": _build("vm", stack, f"{kind}.Dockerfile"), "image": image}}
    }
    (stack / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    return subprocess.run(
        ["docker", "compose", "-p", image, "build", "--progress", "plain"],
        cwd=stack,
        capture_output=True,
        text=True,
    )


def _probe(image: str, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image, "-c", code],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("kind", ["airflow", "mcp"])
def test_the_image_installs_the_in_repo_extension_and_not_its_source(tmp_path, kind):
    require_base_image(kind)
    root = tmp_path / "project"
    root.mkdir()
    config = _project(root, kind)
    stack = root / ".vecten" / "stack"
    write_project_layer(stack, config, None)
    image = f"vecten-in-repo-test-{kind}-{uuid.uuid4().hex[:8]}"
    try:
        build = _build_image(stack, kind, image)
        assert build.returncode == 0, build.stdout + build.stderr
        probe = (
            "import importlib.metadata as m, pathlib, vecten_test_extension as e; "
            f"assert m.version({NAME!r}) == '1.0'; "
            "groups = {p.group for p in m.distribution('vecten-test-extension').entry_points}; "
            "assert 'vecten.dags' in groups and 'vecten.mcp.tools' in groups, groups; "
            "assert 'site-packages' in e.__file__, e.__file__; "
            "assert not pathlib.Path('/tmp/extensions').exists()"
        )
        run = _probe(image, probe)
        assert run.returncode == 0, run.stdout + run.stderr
        assert (root / "extensions" / NAME / "pyproject.toml").is_file()
        assert not list((root / "extensions" / NAME).glob("*.egg-info"))
    finally:
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)


@pytest.mark.parametrize("kind", ["airflow", "mcp"])
def test_extensions_with_conflicting_build_pins_and_a_setuptools_backend_all_install(
    tmp_path, kind
):
    """Two extensions locked at different times pin different hatchling versions, and a third
    is built by setuptools, which writes egg-info and build/ into the tree it builds; the image
    user owns none of the mounted trees."""
    require_base_image(kind)
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(FIXTURE, root / "extensions" / NAME)
    _tiny(root, OLD_HATCHLING, ["hatchling==1.26.3"], "hatchling.build")
    _tiny(root, SETUPTOOLS, ["setuptools>=77"], "setuptools.build_meta")
    names = [NAME, OLD_HATCHLING, SETUPTOOLS]
    config = _config(names)
    _record(root, config, kind, names)
    record = locking.read_record(root)
    hatchling = {
        line
        for name in (NAME, OLD_HATCHLING)
        for line in record[name].build_requires
        if line.startswith("hatchling==")
    }
    assert len(hatchling) == 2, hatchling
    stack = root / ".vecten" / "stack"
    write_project_layer(stack, config, None)
    image = f"vecten-in-repo-pins-{kind}-{uuid.uuid4().hex[:8]}"
    try:
        build = _build_image(stack, kind, image)
        assert build.returncode == 0, build.stdout + build.stderr
        probe = (
            "import importlib.metadata as m, pathlib; "
            f"assert all(m.version(n) == '1.0' for n in {names!r}); "
            "import vecten_old_hatchling, vecten_setuptools_extension as s; "
            "assert 'site-packages' in s.__file__, s.__file__; "
            "assert not any(pathlib.Path(p).exists() for p in "
            "('/tmp/extensions', '/tmp/vecten-build', '/tmp/build-constraints')); "
            # Built from the copy, not from the mounted tree.
            f"url = m.distribution({SETUPTOOLS!r}).read_text('direct_url.json'); "
            "assert 'file:///tmp/vecten-build/' in url, url"
        )
        run = _probe(image, probe)
        assert run.returncode == 0, run.stdout + run.stderr
    finally:
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)


@pytest.mark.parametrize("missing", [False, True], ids=["complete", "missing-file"])
def test_the_mcp_build_packages_a_bound_consumer_skill_offline(tmp_path, missing):
    """The packaging step runs with no network and only its bind-mounted input, so a skill
    that cannot be packaged fails the build rather than the running server."""
    require_base_image("mcp")
    root = tmp_path / "project"
    root.mkdir()
    shutil.copytree(FIXTURE, root / "extensions" / NAME)
    if missing:
        skill = root / "extensions" / NAME / "src" / "vecten_test_extension" / SKILL
        (skill / "references" / "orders.md").unlink()
    config = _config([NAME], consumer_skills={SKILL: {"groups": ["sales"]}})
    _record(root, config, "mcp", [NAME])
    stack = root / ".vecten" / "stack"
    write_project_layer(stack, config, None)
    assert "python -m vecten_shared.consumer_skills" in (stack / "build/mcp.Dockerfile").read_text()
    image = f"vecten-in-repo-skill-{uuid.uuid4().hex[:8]}"
    try:
        build = _build_image(stack, "mcp", image)
        output = build.stdout + build.stderr
        if missing:
            assert build.returncode != 0, output
            assert f"consumer skill {SKILL}: references/orders.md" in output, output
            return
        assert build.returncode == 0, output
        run = _probe(
            image, "import pathlib; assert not pathlib.Path('/tmp/consumer-skills.json').exists()"
        )
        assert run.returncode == 0, run.stdout + run.stderr
    finally:
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True)
