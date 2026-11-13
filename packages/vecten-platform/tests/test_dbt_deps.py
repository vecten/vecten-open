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


UTILS = {"package": "dbt-labs/dbt_utils", "version": "1.3.0"}
SHA = "a" * 40


def _project_packages(dbt_dir, *entries, raw=None):
    text = raw if raw is not None else yaml.safe_dump({"packages": list(entries)})
    (dbt_dir / deps.PROJECT_PACKAGES).write_text(text)


def _refusal(dbt_dir, config=None):
    with pytest.raises(Failure) as exc:
        deps.write_packages_yml(dbt_dir, config or _config())
    assert exc.value.code == ExitCode.CONFIG
    assert not (dbt_dir / "packages.yml").exists()
    return exc.value.message


def test_project_packages_follow_the_generated_local_entries(tmp_path):
    git = {"git": "https://github.com/acme/dbt-helpers.git", "revision": SHA}
    sub = {**git, "subdirectory": "macros_pkg"}
    _project_packages(tmp_path, UTILS, sub)
    doc = yaml.safe_load(deps.write_packages_yml(tmp_path, _config()).read_text())
    assert doc["packages"] == [{"local": str(deps.convention_package_dir())}, UTILS, sub]


def test_the_deps_step_of_every_build_merges_project_packages(tmp_path, monkeypatch):
    # `resolve_dependencies` is the `deps` step of `dbt-build-local` and of the dbt DAG.
    from vecten_platform.dbt import runner

    _project_packages(tmp_path, UTILS)
    written = []

    def fake_run(args, *, project_dir, env, capture=True):
        written.append(yaml.safe_load((project_dir / "packages.yml").read_text()))
        return runner.DbtRun(0, "", "")

    monkeypatch.setattr(runner, "run_dbt", fake_run)
    runner.resolve_dependencies(project_dir=tmp_path, config=_config(), env={}, in_process=False)
    assert written == [{"packages": [{"local": str(deps.convention_package_dir())}, UTILS]}]


def test_no_project_packages_file_writes_the_local_entries_alone(tmp_path):
    doc = yaml.safe_load(deps.write_packages_yml(tmp_path, _config()).read_text())
    assert doc == {"packages": [{"local": str(deps.convention_package_dir())}]}


def test_an_empty_project_packages_file_adds_nothing(tmp_path):
    _project_packages(tmp_path, raw="packages: []\n")
    doc = yaml.safe_load(deps.write_packages_yml(tmp_path, _config()).read_text())
    assert doc == {"packages": [{"local": str(deps.convention_package_dir())}]}


@pytest.mark.parametrize(
    "version",
    [">=1.0.0", [">=1.0.0", "<2.0.0"], "1.3", 1.3, "*", "~1.3.0", "1.3.0 ", "v1.3.0"],
)
def test_an_unpinned_hub_version_is_refused(tmp_path, version):
    _project_packages(tmp_path, {"package": "dbt-labs/dbt_utils", "version": version})
    message = _refusal(tmp_path)
    assert "dbt-labs/dbt_utils" in message and repr(version) in message and "exact" in message


def test_a_hub_package_without_a_version_is_refused(tmp_path):
    _project_packages(tmp_path, {"package": "dbt-labs/dbt_utils"})
    assert "version" in _refusal(tmp_path)


@pytest.mark.parametrize("revision", ["main", "v1.2.0", "a" * 39, "A" * 40, None])
def test_a_git_package_needs_a_full_commit(tmp_path, revision):
    entry = {"git": "https://github.com/acme/dbt-helpers.git"}
    if revision is not None:
        entry["revision"] = revision
    _project_packages(tmp_path, entry)
    message = _refusal(tmp_path)
    assert "https://github.com/acme/dbt-helpers.git" in message and "40" in message


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:acme/dbt-helpers.git",
        "http://github.com/acme/dbt-helpers.git",
        "https://user:token@github.com/acme/dbt-helpers.git",
        "https://{{ env_var('DBT_ENV_SECRET_GIT_TOKEN') }}@github.com/acme/dbt-helpers.git",
        "https://github.com/acme/dbt-helpers.git?private_token=token",
        "https://[github.com/acme/dbt-helpers.git",
    ],
)
def test_a_git_package_must_be_public_https(tmp_path, url):
    _project_packages(tmp_path, {"git": url, "revision": SHA})
    assert "public" in _refusal(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:ghp_synthetic0token@github.com/acme/dbt-helpers.git",
        "https://ghp_synthetic0token@github.com/acme/dbt-helpers.git",
        "https://github.com/acme/dbt-helpers.git?private_token=ghp_synthetic0token",
        "http://ghp_synthetic0token@github.com/acme/dbt-helpers.git",
    ],
)
def test_a_refused_git_url_never_reaches_the_message(tmp_path, url):
    _project_packages(tmp_path, {"git": url, "revision": SHA})
    message = _refusal(tmp_path)
    assert "ghp_synthetic0token" not in message and "github.com" not in message


@pytest.mark.parametrize(
    ("entry", "said"),
    [
        ({"local": "../shared"}, "extension"),
        ({"private": "acme/dbt-helpers", "revision": SHA}, "private"),
        ({"tarball": "https://example.com/p.tgz", "name": "p"}, "tarball"),
        ({**UTILS, "install-prerelease": True}, "install-prerelease"),
        ({"git": "https://github.com/a/b.git", "revision": SHA, "warn-unpinned": False}, "warn"),
        ("dbt-labs/dbt_utils", "mapping"),
    ],
)
def test_entries_other_than_pinned_hub_or_public_git_are_refused(tmp_path, entry, said):
    _project_packages(tmp_path, entry)
    assert said in _refusal(tmp_path)


def test_a_project_entry_named_like_the_convention_package_is_refused(tmp_path):
    _project_packages(tmp_path, {"package": "someone/vecten_convention", "version": "1.0.0"})
    message = _refusal(tmp_path)
    assert "vecten_convention" in message and "generated" in message


def test_a_project_entry_named_like_an_extension_package_is_refused(tmp_path, monkeypatch):
    ext_dir = tmp_path / "ext_dbt"
    ext_dir.mkdir()
    (ext_dir / "dbt_project.yml").write_text("name: acme_resolution\nversion: '1.0.0'\n")
    monkeypatch.setattr(deps, "extension_packages", lambda config: [("acme_dbt", ext_dir)])
    entry = {"git": "https://github.com/acme/acme_resolution.git", "revision": SHA}
    _project_packages(tmp_path, entry)
    message = _refusal(tmp_path, _config(extensions=["acme-resolution"]))
    assert "acme_resolution" in message and str(ext_dir) in message


def test_a_package_listed_twice_is_refused(tmp_path):
    _project_packages(tmp_path, UTILS, {**UTILS, "version": "1.2.0"})
    assert "twice" in _refusal(tmp_path)


DBT_UTILS_1_3_0 = "e164c645238ca98f21b2ecc558912b94b3d75a9f"


@pytest.mark.network
@pytest.mark.parametrize(
    "entry",
    [
        UTILS,
        {"git": "https://github.com/dbt-labs/dbt-utils.git", "revision": DBT_UTILS_1_3_0},
    ],
)
def test_dbt_deps_installs_a_pinned_project_package(tmp_path, entry):
    from vecten_platform.dbt import runner

    (tmp_path / "dbt_project.yml").write_text(
        "name: probe\nversion: '1.0.0'\nconfig-version: 2\nprofile: probe\n"
    )
    (tmp_path / "profiles.yml").write_text(
        "probe:\n  target: t\n  outputs:\n    t:\n      type: postgres\n      host: localhost\n"
        "      port: 5432\n      user: u\n      password: p\n      dbname: d\n      schema: s\n"
    )
    _project_packages(tmp_path, entry)
    runner.resolve_dependencies(
        project_dir=tmp_path,
        config=_config(),
        env=runner.base_env(tmp_path, {}),
        in_process=False,
    )
    installed = tmp_path / "dbt_packages"
    assert (installed / "dbt_utils" / "macros").is_dir()
    assert (installed / deps.CONVENTION_PACKAGE).is_dir()
    locked = yaml.safe_load((tmp_path / "package-lock.yml").read_text())["packages"]
    assert {k: v for k, v in locked[-1].items() if k in entry} == entry


@pytest.mark.parametrize(
    "raw",
    ["packages: [", "- package: x\n", "packages: {}\n", "other: []\n", "packages: []\nx: 1\n"],
)
def test_a_malformed_project_packages_file_is_refused(tmp_path, raw):
    _project_packages(tmp_path, raw=raw)
    assert deps.PROJECT_PACKAGES in _refusal(tmp_path)
