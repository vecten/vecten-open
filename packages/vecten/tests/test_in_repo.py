"""Which paths `extension add` accepts as in-repo extensions, and what it reads from them."""

import hashlib
import os

import pytest

from vecten import in_repo
from vecten_shared.output import ExitCode, Failure

PYPROJECT = """[project]
name = "{name}"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["httpx>=0.27"]

[project.optional-dependencies]
platform = ["polars>=1,<2"]

[project.entry-points."vecten.skills"]
portal = "acme_portal:skill"

[build-system]
requires = ["hatchling==1.26.3"]
build-backend = "hatchling.build"
"""


def make(root, directory="acme-portal", name="acme-portal", text=PYPROJECT):
    path = root / "extensions" / directory
    (path / "src" / "acme_portal").mkdir(parents=True)
    (path / "src" / "acme_portal" / "__init__.py").write_text("skill = None\n")
    (path / "pyproject.toml").write_text(text.format(name=name))
    return path


def test_path_arguments_are_told_apart_from_package_names():
    for arg in ("./extensions/acme-portal", "extensions/acme-portal", "/abs/x", "../x"):
        assert in_repo.is_path(arg)
    for arg in ("acme-portal", "acme-portal[cli]==1.0", "acme.portal>=1"):
        assert not in_repo.is_path(arg)


@pytest.mark.parametrize(
    "arg", ["./extensions/acme-portal", "extensions/acme-portal", "extensions/acme-portal/"]
)
def test_an_extension_under_the_folder_is_read(tmp_path, arg):
    path = make(tmp_path)
    found = in_repo.check(tmp_path, arg)
    assert found.name == "acme-portal"
    assert found.version == "0.1.0"
    assert found.path == "extensions/acme-portal"
    assert found.extras == ("platform",)
    assert found.build_requires == ("hatchling==1.26.3",)
    assert found.groups == ("vecten.skills",)
    expected = hashlib.sha256((path / "pyproject.toml").read_bytes()).hexdigest()
    assert found.metadata_sha256 == expected


def test_an_absolute_path_inside_the_project_is_read(tmp_path):
    path = make(tmp_path)
    assert in_repo.check(tmp_path, str(path)).name == "acme-portal"


@pytest.mark.parametrize(
    "arg",
    [
        "../shared/acme-portal",
        "./tools/acme-portal",
        "./extensions/acme-portal/../acme-portal",
        "./extensions",
        "./extensions/acme-portal/src",
    ],
)
def test_a_path_outside_the_folder_is_refused(tmp_path, arg):
    make(tmp_path)
    (tmp_path / "tools" / "acme-portal").mkdir(parents=True)
    with pytest.raises(Failure) as caught:
        in_repo.check(tmp_path, arg)
    assert caught.value.code == ExitCode.USAGE
    assert "extensions/<name>/" in caught.value.message


def test_an_absolute_path_outside_the_project_is_refused(tmp_path):
    outside = make(tmp_path / "elsewhere")
    with pytest.raises(Failure, match="extensions/<name>/"):
        in_repo.check(tmp_path / "project", str(outside))


def test_a_symbolic_link_anywhere_under_it_is_refused(tmp_path):
    path = make(tmp_path)
    (path / "src" / "acme_portal" / "static").symlink_to(tmp_path)
    with pytest.raises(Failure, match="static"):
        in_repo.check(tmp_path, "./extensions/acme-portal")


def test_a_linked_extensions_folder_is_refused(tmp_path):
    real = make(tmp_path / "real")
    (tmp_path / "project").mkdir()
    os.symlink(real.parent, tmp_path / "project" / "extensions")
    with pytest.raises(Failure, match="symbolic link"):
        in_repo.check(tmp_path / "project", "./extensions/acme-portal")


def test_the_directory_must_carry_the_distribution_name(tmp_path):
    make(tmp_path, directory="portal", name="acme-portal")
    with pytest.raises(Failure, match="extensions/acme-portal"):
        in_repo.check(tmp_path, "./extensions/portal")


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (PYPROJECT.replace('version = "0.1.0"\n', 'dynamic = ["version"]\n'), "version"),
        (PYPROJECT.split("[build-system]")[0], "build-system"),
        ("not toml [", "pyproject.toml"),
    ],
)
def test_metadata_must_be_static_and_declare_its_build(tmp_path, text, said):
    make(tmp_path, text=text)
    with pytest.raises(Failure, match=said):
        in_repo.check(tmp_path, "./extensions/acme-portal")


def test_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(Failure, match="acme-portal"):
        in_repo.check(tmp_path, "./extensions/acme-portal")


@pytest.mark.parametrize(
    "field", ["dependencies", "optional-dependencies", "entry-points", "scripts", "gui-scripts"]
)
def test_metadata_that_decides_resolution_or_wiring_must_be_static(tmp_path, field):
    make(tmp_path, text=PYPROJECT.replace("[project]\n", f'[project]\ndynamic = ["{field}"]\n'))
    with pytest.raises(Failure, match=field):
        in_repo.check(tmp_path, "./extensions/acme-portal")
