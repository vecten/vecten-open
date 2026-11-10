import pytest
import yaml

from vecten_platform.dbt import deps
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import VERSIONS


def _config(**over):
    base = {"users": 2, "project": {"name": "acme", "scaffold_version": VERSIONS.vecten}}
    return VectenConfig.model_validate({**base, **over})


def test_convention_package_is_bundled_at_the_table_version():
    assert (deps.convention_package_dir() / "dbt_project.yml").is_file()
    assert deps.convention_version() == VERSIONS.vecten_convention


def test_write_packages_yml_lists_local_paths(tmp_path):
    path = deps.write_packages_yml(tmp_path, _config())
    doc = yaml.safe_load(path.read_text())
    assert doc == {"packages": [{"local": str(deps.convention_package_dir())}]}
    assert f"vecten_convention {VERSIONS.vecten_convention}" in path.read_text()


def test_write_packages_yml_adds_extension_packages(tmp_path, monkeypatch):
    ext_dir = tmp_path / "ext_dbt"
    ext_dir.mkdir()
    monkeypatch.setattr(deps, "extension_packages", lambda config: [("acme_resolution", ext_dir)])
    doc = yaml.safe_load(
        deps.write_packages_yml(tmp_path, _config(extensions=["acme-resolution"])).read_text()
    )
    assert doc["packages"][1] == {"local": str(ext_dir)}


def test_write_packages_yml_refuses_version_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "convention_version", lambda: "9.9.9")
    with pytest.raises(Failure) as exc:
        deps.write_packages_yml(tmp_path, _config())
    assert exc.value.code == ExitCode.CONFIG and "vecten_convention" in exc.value.message


def test_extension_packages_reads_entry_points(tmp_path, monkeypatch):
    class EP:
        name = "acme_resolution"
        group = "vecten.dbt_packages"
        dist = type("D", (), {"name": "acme-resolution", "version": "1.0"})()

        def load(self):
            return tmp_path

    monkeypatch.setattr(deps, "_entry_points", lambda: [EP()])
    assert deps.extension_packages(_config(extensions=["acme-resolution"])) == [
        ("acme_resolution", tmp_path)
    ]
    assert deps.extension_packages(_config(extensions=["acme-resolution>=1,<2"])) == [
        ("acme_resolution", tmp_path)
    ]
    assert deps.extension_packages(_config()) == []


def test_extension_packages_matches_normalised_names(tmp_path, monkeypatch):
    class EP:
        name = "acme_resolution"
        dist = type("D", (), {"name": "Acme_Resolution"})()

        def load(self):
            return str(tmp_path)

    monkeypatch.setattr(deps, "_entry_points", lambda: [EP()])
    assert deps.extension_packages(_config(extensions=["acme.resolution[extra]"])) == [
        ("acme_resolution", tmp_path)
    ]


def test_extension_packages_refuses_a_missing_directory(tmp_path, monkeypatch):
    class EP:
        name = "acme_resolution"
        dist = type("D", (), {"name": "acme-resolution", "version": "1.0"})()

        def load(self):
            return tmp_path / "absent"

    monkeypatch.setattr(deps, "_entry_points", lambda: [EP()])
    with pytest.raises(Failure) as exc:
        deps.extension_packages(_config(extensions=["acme-resolution"]))
    assert exc.value.code == ExitCode.CONFIG and "acme_resolution" in exc.value.message
