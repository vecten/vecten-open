"""Docker test helpers leave no per-project images behind."""

import subprocess

import pytest

from vecten_shared.config import stack_project_name
from vecten_shared.versions import VERSIONS

from . import docker_util

pytestmark = pytest.mark.docker


def image_exists(reference):
    return (
        subprocess.run(["docker", "image", "inspect", reference], capture_output=True).returncode
        == 0
    )


def test_stack_images_of_one_project_are_removed_and_shared_images_kept(tmp_path):
    if not docker_util.docker_available():
        pytest.skip("Docker unavailable")
    base = f"postgres:{VERSIONS.postgres_major}"
    if not image_exists(base):
        subprocess.run(["docker", "pull", base], capture_output=True, check=True)
    project = stack_project_name(tmp_path, "acme")
    other = stack_project_name(tmp_path / "elsewhere", "acme")
    built = [f"{project}-airflow:{VERSIONS.vecten}", f"{project}-mcp:{VERSIONS.vecten}"]
    kept = f"{other}-airflow:{VERSIONS.vecten}"
    for reference in (*built, kept):
        subprocess.run(["docker", "tag", base, reference], capture_output=True, check=True)
    try:
        docker_util.remove_stack_images(project)
        assert [reference for reference in built if image_exists(reference)] == []
        assert image_exists(kept) and image_exists(base)
    finally:
        subprocess.run(["docker", "image", "rm", *built, kept], capture_output=True)
