from pathlib import Path

import pytest
import yaml

from vecten.cli.commands import dbt as dbt_cmd
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_platform.dbt.runner import DbtRun

from .fixture_project import render_fixture


def test_dbt_forwards_arguments_and_exit_code(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    monkeypatch.setenv("VECTEN_OSI_PATH", "/elsewhere")
    (root / ".env").write_text("LOADER_PASSWORD='pw'\n")
    seen = {}

    def fake_run(args, *, project_dir, env, capture=True):
        seen.update(args=args, project_dir=project_dir, env=env, capture=capture)
        return DbtRun(7, "", "")

    monkeypatch.setattr(dbt_cmd, "run_dbt", fake_run)
    result = invoke("dbt", "run", "--select", "core", "--json")
    assert result.exit_code == 7
    assert result.output == ""
    assert seen["args"] == ["run", "--select", "core", "--json"] and seen["capture"] is False
    assert seen["project_dir"] == root / "dbt" and seen["env"]["WAREHOUSE_PASSWORD"] == "pw"
    assert seen["env"]["DBT_PROFILES_DIR"] == str(root / "dbt")
    assert "vecten_convention" in (root / "dbt" / "packages.yml").read_text()
    assert "VECTEN_OSI_PATH" not in seen["env"]


def test_dbt_refuses_a_drifted_convention_package(invoke, tmp_path, monkeypatch):
    from vecten_platform.dbt import deps

    calls = _recording_project(tmp_path, monkeypatch)
    monkeypatch.setattr(deps, "convention_version", lambda: "0.0.0")
    result = invoke("dbt", "run")
    assert result.exit_code == 6
    assert calls == []
    assert "reinstall vecten" in result.stderr


def test_dbt_merges_the_project_packages_before_running(invoke, tmp_path, monkeypatch):
    calls = _recording_project(tmp_path, monkeypatch)
    listed = "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n"
    Path("dbt/project-packages.yml").write_text(listed)
    result = invoke("dbt", "deps")
    assert result.exit_code == 0, result.stderr
    assert calls == [["deps"]]
    packages = yaml.safe_load(Path("dbt/packages.yml").read_text())["packages"]
    assert packages[-1] == {"package": "dbt-labs/dbt_utils", "version": "1.3.0"}


def test_dbt_refuses_an_unpinned_project_package(invoke, tmp_path, monkeypatch):
    calls = _recording_project(tmp_path, monkeypatch)
    listed = "packages:\n  - package: dbt-labs/dbt_utils\n    version: '>=1.0.0'\n"
    Path("dbt/project-packages.yml").write_text(listed)
    result = invoke("dbt", "deps")
    assert result.exit_code == 6
    assert calls == []
    assert "dbt/project-packages.yml" in result.stderr and "exact" in result.stderr


def _recording_project(tmp_path, monkeypatch, warehouse="postgres"):
    root = render_fixture(tmp_path, warehouse)
    monkeypatch.chdir(root)
    (root / ".env").write_text("LOADER_PASSWORD='pw'\n")
    calls = []

    def fake_run(args, *, project_dir, env, capture=True):
        calls.append(args)
        return DbtRun(0, "", "")

    monkeypatch.setattr(dbt_cmd, "run_dbt", fake_run)
    return calls


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--help"],
        ["--help"],
        ["run", "--", "--x"],
        ["--", "run"],
        ["run", "--select", "core", "--", "--full-refresh", "--"],
    ],
)
def test_dbt_interprets_no_argument_of_its_own(invoke, tmp_path, monkeypatch, args):
    calls = _recording_project(tmp_path, monkeypatch)
    result = invoke("dbt", *args)
    assert result.exit_code == 0, result.output
    assert calls == [args]


def test_root_help_lists_dbt_with_its_help(invoke):
    result = invoke("--help")
    assert result.exit_code == 0
    assert "Run dbt against the project's profile" in result.stdout


def test_dbt_on_a_snowflake_project_points_at_env_and_the_local_build(
    invoke, tmp_path, monkeypatch
):
    calls = _recording_project(tmp_path, monkeypatch, warehouse="snowflake")
    result = invoke("dbt", "debug")
    assert result.exit_code == 3
    assert calls == []
    assert result.stderr == (
        "error: vecten dbt without --env runs against the laptop stack, which has no "
        "Snowflake: run `vecten dbt --env <env> <args>` with a deployed environment's "
        "credentials, or `vecten dbt-build-local` for the credential-free build on the "
        "Snowflake emulation\n"
    )


def test_dbt_outside_a_project_is_refused(invoke):
    result = invoke("dbt", "debug", "--json")
    assert result.exit_code == 4
    assert result.stdout == ""
    assert result.stderr.startswith("error: not inside a vecten project")


def test_dbt_refuses_mcp_only_project(invoke, tmp_path, monkeypatch):
    answers = {"name": "acme", "users": 2, "warehouse_url": "postgres://h:5432/dw"}
    render_project(tmp_path / "p", ScaffoldParams.from_answers(answers, {}))
    monkeypatch.chdir(tmp_path / "p")
    result = invoke("dbt", "debug")
    assert result.exit_code == 4
    assert "platform" in result.stderr


def test_dbt_without_the_loader_secret_names_the_command(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    result = invoke("dbt", "debug")
    assert result.exit_code == 3
    assert "vecten secret set LOADER_PASSWORD" in result.stderr


def _declare_finance(root):
    from vecten_shared.config import AreaDecl, load_config, save_config

    config = load_config(root)
    save_config(root, config.model_copy(update={"areas": {"finance": AreaDecl()}}))


AREAS = '{"vecten_areas": ["finance"]}'


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["test"], ["test", "--vars", AREAS]),
        (
            ["--debug", "build", "--select", "marts"],
            ["--debug", "build", "--select", "marts", "--vars", AREAS],
        ),
        (["docs", "generate"], ["docs", "generate", "--vars", AREAS]),
        (
            ["test", "--vars", "{audit: true}"],
            ["test", "--vars", '{"audit": true, "vecten_areas": ["finance"]}'],
        ),
        (
            ["test", "--vars={audit: true}"],
            ["test", '--vars={"audit": true, "vecten_areas": ["finance"]}'],
        ),
        (
            ["test", "--vars", "{vecten_areas: [other]}"],
            ["test", "--vars", "{vecten_areas: [other]}"],
        ),
        (["deps"], ["deps", "--vars", AREAS]),
        (["--version"], ["--version"]),
    ],
    ids=["test", "global-flag", "docs", "merged", "merged-equals", "own-areas", "deps", "version"],
)
def test_dbt_passes_the_declared_areas_to_commands_that_take_vars(
    invoke, tmp_path, monkeypatch, args, expected
):
    calls = _recording_project(tmp_path, monkeypatch)
    _declare_finance(Path.cwd())
    result = invoke("dbt", *args)
    assert result.exit_code == 0, result.stderr
    assert calls == [expected]


def test_dbt_without_areas_passes_no_vars(invoke, tmp_path, monkeypatch):
    calls = _recording_project(tmp_path, monkeypatch)
    assert invoke("dbt", "test").exit_code == 0
    assert calls == [["test"]]
