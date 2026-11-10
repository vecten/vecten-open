"""The MCP base image as published: non-root, no secrets, the server and nothing of the CLI."""

import json
import re
import shutil
import subprocess
import tomllib
import uuid

import pytest

from vecten.stack.images import write_project_layer
from vecten_shared.config import VectenConfig
from vecten_shared.versions import VERSIONS

from .docker_util import LOCK_INPUTS, REPO, docker_available, ensure_mcp_image

pytestmark = pytest.mark.docker
SECRET_NAME = re.compile(r"\w*(?:PASSWORD|SECRET|TOKEN|PRIVATE|_KEY)\w*", re.IGNORECASE)
# The python base image's release-signing key fingerprint, which is public.
PUBLIC_NAMES = {"GPG_KEY"}


def secret_names(text: str) -> set[str]:
    """Secret-looking names assigned in `text` (`NAME=value`), beyond the public ones."""
    return {m.group(0) for m in SECRET_NAME.finditer(text) if text[m.end() : m.end() + 1] == "="}


def image_config(image: str) -> dict:
    out = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)[0]["Config"]


def run_in(image: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", argv[0], image, *argv[1:]],
        capture_output=True,
        text=True,
    )


def test_base_image_runs_the_server_as_an_unprivileged_user_without_secrets():
    image = ensure_mcp_image()
    config = image_config(image)
    assert config["User"] == "app"
    assert config["Cmd"] == ["python", "-m", "vecten_mcp"]
    assert "8001/tcp" in config["ExposedPorts"]
    assert secret_names("\n".join(config["Env"])) <= PUBLIC_NAMES, config["Env"]
    history = subprocess.run(
        ["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert secret_names(history) <= PUBLIC_NAMES, history
    listing = run_in(
        image,
        "sh",
        "-c",
        "stat -c '%U %a' /var/lib/vecten-mcp/secrets; ls -A /var/lib/vecten-mcp/secrets | wc -l",
    )
    assert listing.stdout.split() == ["app", "700", "0"], listing


def test_base_image_holds_the_server_packages_and_never_the_cli():
    image = ensure_mcp_image()
    probe = run_in(
        image,
        "python",
        "-c",
        "import json, importlib.metadata as m, importlib.util as u;"
        "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()"
        " if d.metadata['Name'].startswith('vecten')}));"
        "print(u.find_spec('vecten') is None, u.find_spec('vecten_platform') is None)",
    )
    assert probe.returncode == 0, probe.stderr
    dists, absent = probe.stdout.splitlines()
    assert json.loads(dists) == {"vecten-mcp": VERSIONS.vecten, "vecten-shared": VERSIONS.vecten}
    assert absent == "True True"


def test_base_image_holds_the_versions_the_lock_pins():
    image = ensure_mcp_image()
    probe = run_in(
        image,
        "python",
        "-c",
        "import json, importlib.metadata as m;"
        "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))",
    )
    assert probe.returncode == 0, probe.stderr
    installed = {canonical(name): version for name, version in json.loads(probe.stdout).items()}
    locked: dict[str, set[str]] = {}
    for package in tomllib.loads((REPO / "uv.lock").read_text())["package"]:
        locked.setdefault(canonical(package["name"]), set()).add(package["version"])
    third_party = set(installed) - {"vecten-mcp", "vecten-shared", "pip"}
    assert "mcp" in third_party
    off_lock = {
        name: (installed[name], sorted(locked.get(name, ())))
        for name in third_party
        if installed[name] not in locked.get(name, ())
    }
    assert off_lock == {}


def test_base_image_keeps_pip_for_the_project_layer_and_holds_no_uv():
    image = ensure_mcp_image()
    # As root, so no directory hides a uv binary or the lock file uv takes on an environment.
    probe = subprocess.run(
        ["docker", "run", "--rm", "--user", "0", "--entrypoint", "sh", image, "-c"]
        + [
            "command -v pip; find / -xdev \\( -name uv -o -name .lock \\) ! -type d -print;"
            " find /opt/venv /var/lib/vecten-mcp ! -user app -print"
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.split() == ["/opt/venv/bin/pip"]


def test_base_image_build_refuses_a_stale_lock(tmp_path):
    if not docker_available():
        pytest.skip("Docker is not available")
    for name in LOCK_INPUTS:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, tmp_path / name)
    (tmp_path / "dist").mkdir()
    member = tmp_path / "packages/vecten-mcp/pyproject.toml"
    text = member.read_text()
    assert "dependencies = [\n" in text
    member.write_text(text.replace("dependencies = [\n", 'dependencies = [\n    "rich>=13",\n', 1))
    tag = f"vecten-mcp-stale-lock-test:{uuid.uuid4().hex[:10]}"
    try:
        built = subprocess.run(
            ["docker", "build", "-f", str(REPO / "images/mcp/Dockerfile")]
            + ["--build-arg", f"BASE_IMAGE={VERSIONS.mcp_base_image}", "-t", tag, str(tmp_path)],
            capture_output=True,
            text=True,
        )
        assert built.returncode != 0
        assert "The lockfile at `uv.lock` needs to be updated" in built.stderr, built.stderr[-3000:]
        listed = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
        assert listed.returncode != 0
    finally:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_project_layer_keeps_the_base_user_and_command(tmp_path):
    image = ensure_mcp_image()
    config = VectenConfig.model_validate(
        {
            "project": {"name": "layer", "scaffold_version": "0.1.0"},
            "users": 2,
            "components": ["mcp"],
        }
    )
    stack = tmp_path / ".vecten/stack"
    stack.mkdir(parents=True)
    write_project_layer(stack, config, None)
    tag = f"vecten-mcp-layer-test:{uuid.uuid4().hex[:10]}"
    try:
        built = subprocess.run(
            ["docker", "build", "-f", str(stack / "build/mcp.Dockerfile"), "-t", tag]
            + [str(stack / "build")],
            capture_output=True,
            text=True,
        )
        assert built.returncode == 0, built.stderr[-2000:]
        layered, base = image_config(tag), image_config(image)
        assert (layered["User"], layered["Cmd"]) == (base["User"], base["Cmd"])
    finally:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)
