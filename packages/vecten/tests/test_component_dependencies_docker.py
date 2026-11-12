"""Each image's project layer installs its own component's lock and nothing from the other's."""

import subprocess
import uuid

import pytest

from vecten import locking
from vecten.stack.images import base_image, write_project_layer
from vecten_shared.config import VectenConfig, save_config
from vecten_shared.versions import VERSIONS

from .docker_util import docker_available

pytestmark = pytest.mark.docker
# Small pure-Python distributions neither base image installs.
PACKAGES = {"airflow": "cowsay==6.1", "mcp": "pyjokes==0.8.3"}
MODULES = {"airflow": "cowsay", "mcp": "pyjokes"}


def _stamped(root, config, component, lines):
    roots = locking.roots(config, {}, {}, component)
    stamp = locking.stamp_line(component, locking.inputs_hash(component, roots))
    (root / locking.LOCK_FILES[component]).write_text(
        stamp + "\n" + "".join(f"{x}\n" for x in lines)
    )


def test_each_image_holds_only_its_components_packages(tmp_path):
    if not docker_available():
        pytest.skip("Docker is not available")
    for kind in PACKAGES:
        if subprocess.run(
            ["docker", "image", "inspect", base_image(kind, None)], capture_output=True
        ).returncode:
            pytest.skip(f"build {base_image(kind, None)} with vecten dev build-images first")
    config = VectenConfig.model_validate(
        {
            "project": {"name": "components-test", "scaffold_version": VERSIONS.vecten},
            "users": 2,
            "dependencies": {
                "platform": {"python": [PACKAGES["airflow"]]},
                "mcp": {"python": [PACKAGES["mcp"]]},
            },
        }
    )
    save_config(tmp_path, config)
    (tmp_path / locking.PINS_FILE).write_text("")
    (tmp_path / locking.RECORD_FILE).write_text(locking.record_text({}))
    _stamped(tmp_path, config, "cli", [])
    _stamped(tmp_path, config, "platform", [PACKAGES["airflow"]])
    _stamped(tmp_path, config, "mcp", [PACKAGES["mcp"]])
    stack = tmp_path / ".vecten" / "stack"
    write_project_layer(stack, config, None)
    context = stack / "build"
    images = []
    try:
        for kind in PACKAGES:
            name = f"vecten-components-test-{kind}-{uuid.uuid4().hex[:8]}"
            build = subprocess.run(
                ["docker", "build", "-q", "-t", name, "-f", str(context / f"{kind}.Dockerfile")]
                + [str(context)],
                capture_output=True,
                text=True,
            )
            assert build.returncode == 0, build.stdout + build.stderr
            images.append(name)
            other = "mcp" if kind == "airflow" else "airflow"
            probe = (
                "import importlib.util, os; assert os.getuid() != 0; "
                f"assert importlib.util.find_spec({MODULES[kind]!r}); "
                f"assert importlib.util.find_spec({MODULES[other]!r}) is None"
            )
            run = subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "--entrypoint", "python", name]
                + ["-c", probe],
                capture_output=True,
                text=True,
            )
            assert run.returncode == 0, run.stdout + run.stderr
    finally:
        for name in images:
            subprocess.run(["docker", "image", "rm", "-f", name], capture_output=True)


@pytest.mark.parametrize(("kind", "component"), [("airflow", "platform"), ("mcp", "mcp")])
def test_a_system_package_is_installed_without_apt_lists_and_the_image_user_stays(
    tmp_path, kind, component
):
    if not docker_available():
        pytest.skip("Docker is not available")
    if subprocess.run(
        ["docker", "image", "inspect", base_image(kind, None)], capture_output=True
    ).returncode:
        pytest.skip(f"build {base_image(kind, None)} with vecten dev build-images first")
    config = VectenConfig.model_validate(
        {
            "project": {"name": "system-test", "scaffold_version": VERSIONS.vecten},
            "users": 2,
            "components": [component],
            # A tiny package neither base image installs.
            "dependencies": {component: {"system": ["sl"]}},
        }
    )
    save_config(tmp_path, config)
    stack = tmp_path / ".vecten" / "stack"
    write_project_layer(stack, config, None)
    context = stack / "build"
    name = f"vecten-system-test-{kind}-{uuid.uuid4().hex[:8]}"
    try:
        build = subprocess.run(
            ["docker", "build", "-q", "-t", name, "-f", str(context / f"{kind}.Dockerfile")]
            + [str(context)],
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        user = "airflow" if kind == "airflow" else "app"
        probe = (
            f'test "$(id -un)" = {user} && dpkg -s sl >/dev/null '
            '&& test -z "$(ls -A /var/lib/apt/lists 2>/dev/null)"'
        )
        run = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", name]
            + ["-c", probe],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stdout + run.stderr
    finally:
        subprocess.run(["docker", "image", "rm", "-f", name], capture_output=True)
