import json
import os
import subprocess
import sys

import pytest

from vecten_platform.dbt import runner
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import VERSIONS


def test_dbt_argv_runs_dbt_through_this_interpreter():
    argv = runner.dbt_argv(["build", "--select", "core"])
    assert argv[0] == sys.executable
    assert argv[-3:] == ["build", "--select", "core"]


def test_dbt_starts_without_a_runtime_warning():
    # Every Airflow dbt task logs what dbt prints, so a start-up warning lands in each one.
    proc = subprocess.run(
        runner.dbt_argv(["--version"]),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONWARNINGS": "error::RuntimeWarning"},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "RuntimeWarning" not in proc.stderr


def test_dbt_argv_passes_dbt_its_exit_code():
    proc = subprocess.run(
        runner.dbt_argv(["debug", "--no-such-option"]), capture_output=True, check=False
    )
    assert proc.returncode == 2


def test_base_env_points_dbt_at_the_project(tmp_path, monkeypatch):
    monkeypatch.setenv("DBT_PROFILES_DIR", "/elsewhere")
    env = runner.base_env(tmp_path, {"WAREHOUSE_HOST": "h"})
    assert env["DBT_PROJECT_DIR"] == str(tmp_path) and env["DBT_PROFILES_DIR"] == str(tmp_path)
    assert env["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "0" and env["WAREHOUSE_HOST"] == "h"
    assert env["DBT_LOG_PATH"] == str(tmp_path / "logs")


def test_failed_nodes_reads_run_results(tmp_path):
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "run_results.json").write_text(
        json.dumps(
            {
                "results": [
                    {"unique_id": "model.p.company", "status": "success", "message": None},
                    {
                        "unique_id": "test.p.convention_descriptions",
                        "status": "fail",
                        "message": "model company: column x has no description",
                        "failures": 1,
                    },
                    {"unique_id": "model.p.deal", "status": "error", "message": "relation missing"},
                ]
            }
        )
    )
    failed = runner.failed_nodes(tmp_path)
    assert [f["name"] for f in failed] == ["convention_descriptions", "deal"]
    assert failed[0]["status"] == "fail" and "no description" in failed[0]["message"]
    assert runner.failed_nodes(tmp_path / "nowhere") == []


def test_run_build_is_two_steps(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, *, project_dir, env, capture=True):
        calls.append(args)
        return runner.DbtRun(0, "", "")

    monkeypatch.setattr(runner, "run_dbt", fake_run)
    runs = runner.run_build(project_dir=tmp_path, env={}, in_process=False)
    assert calls == [
        ["build", "--exclude", "package:vecten_convention,test_type:singular"],
        ["test", "--select", "package:vecten_convention,test_type:singular"],
    ]
    assert len(runs) == 2


def test_run_build_passes_the_declared_areas_to_both_steps(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, *, project_dir, env, capture=True):
        calls.append(args)
        return runner.DbtRun(0, "", "")

    monkeypatch.setattr(runner, "run_dbt", fake_run)
    runner.run_build(project_dir=tmp_path, env={}, in_process=False, areas={"payroll", "finance"})
    expected = ["--vars", '{"vecten_areas": ["finance", "payroll"]}']
    assert [call[-2:] for call in calls] == [expected, expected]
    assert runner.area_vars([]) == ()


def test_run_build_stops_after_a_failed_build(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "run_dbt", lambda args, **kw: runner.DbtRun(1, "boom", ""))
    runs = runner.run_build(project_dir=tmp_path, env={}, in_process=False)
    assert len(runs) == 1 and runs[0].returncode == 1


def test_run_build_in_process_wraps_the_outcome(monkeypatch, tmp_path):
    outcomes = iter([runner.DbtRun(0, "", ""), runner.DbtRun(1, "", "boom")])
    monkeypatch.setattr(runner, "invoke_dbt", lambda args, **kw: next(outcomes))
    runs = runner.run_build(project_dir=tmp_path, env={}, in_process=True)
    assert runs == [runner.DbtRun(0, "", ""), runner.DbtRun(1, "", "boom")]


def test_run_dbt_runs_in_the_project_dir(tmp_path):
    run = runner.run_dbt(["--version"], project_dir=tmp_path, env=runner.base_env(tmp_path, {}))
    assert run.returncode == 0 and "1.12" in run.stdout


def test_resolve_dependencies_reports_the_tail_of_dbt_output(monkeypatch, tmp_path):
    seen = []

    def fake_run(args, *, project_dir, env, capture=True):
        seen.append(args)
        return runner.DbtRun(2, "x" * 3000 + "stdout tail", "")

    monkeypatch.setattr(runner, "run_dbt", fake_run)
    config = VectenConfig.model_validate(
        {"users": 2, "project": {"name": "acme", "scaffold_version": VERSIONS.vecten}}
    )
    with pytest.raises(Failure) as exc:
        runner.resolve_dependencies(project_dir=tmp_path, config=config, env={}, in_process=False)
    assert seen == [["deps"]] and (tmp_path / "packages.yml").is_file()
    assert exc.value.code == ExitCode.TOOL_FAILED
    assert exc.value.message.endswith("stdout tail") and len(exc.value.message) < 2100


def test_invoke_dbt_closes_the_log_file_it_opened(tmp_path):
    import logging

    # An empty directory fails to parse only after dbt has opened its log file.
    run = runner.invoke_dbt(["parse"], project_dir=tmp_path, env=runner.base_env(tmp_path, {}))
    assert run.returncode == 1 and "dbt_project.yml" in run.stderr
    assert (tmp_path / "logs" / "dbt.log").is_file()
    assert logging.getLogger("file_log").handlers == []


def test_process_state_restores_after_a_missing_project_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before_env, before_cwd = dict(os.environ), os.getcwd()
    with pytest.raises(FileNotFoundError):
        with runner._process_state(tmp_path / "absent", {"WAREHOUSE_PASSWORD": "s3cret"}):
            pass
    assert dict(os.environ) == before_env and os.getcwd() == before_cwd


def test_process_state_restores_after_a_raising_body(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    before_env, before_cwd = dict(os.environ), os.getcwd()
    with pytest.raises(RuntimeError):
        with runner._process_state(project, {"WAREHOUSE_PASSWORD": "s3cret"}):
            assert dict(os.environ) == {"WAREHOUSE_PASSWORD": "s3cret"}
            assert os.getcwd() == str(project.resolve())
            raise RuntimeError("dbt blew up")
    assert dict(os.environ) == before_env and os.getcwd() == before_cwd


def test_base_env_keeps_the_file_log_and_reads_the_root_osi(tmp_path, monkeypatch):
    monkeypatch.setenv(runner.OSI_PATH_VAR, "/somewhere/empty")
    env = runner.base_env(tmp_path, {})
    assert env["DBT_LOG_LEVEL_FILE"] == "debug"
    assert runner.OSI_PATH_VAR not in env


def test_output_tail_keeps_both_streams():
    run = runner.DbtRun(1, "progress\nCompilation Error in model x", "a warning")
    assert runner.output_tail(run) == "progress\nCompilation Error in model x\na warning"
    assert runner.output_tail(runner.DbtRun(1, "", "")) == ""
    assert runner.output_tail(runner.DbtRun(1, "x" * 50, "y" * 50), limit=10) == (
        "x" * 10 + "\n" + "y" * 10
    )


def _config() -> VectenConfig:
    return VectenConfig.model_validate(
        {"users": 2, "project": {"name": "acme", "scaffold_version": VERSIONS.vecten}}
    )


def test_resolve_dependencies_reports_both_streams(monkeypatch, tmp_path):
    def fake_run(args, *, project_dir, env, capture=True):
        return runner.DbtRun(2, "Encountered an error: package not found", "DeprecationWarning")

    monkeypatch.setattr(runner, "run_dbt", fake_run)
    with pytest.raises(Failure) as exc:
        runner.resolve_dependencies(
            project_dir=tmp_path, config=_config(), env={}, in_process=False
        )
    assert "package not found" in exc.value.message
    assert "DeprecationWarning" in exc.value.message


def test_resolve_dependencies_in_process_reports_the_exception(monkeypatch, tmp_path):
    failed = runner.DbtRun(1, "", "Runtime Error: no profile")
    monkeypatch.setattr(runner, "invoke_dbt", lambda args, **kw: failed)
    with pytest.raises(Failure) as exc:
        runner.resolve_dependencies(project_dir=tmp_path, config=_config(), env={}, in_process=True)
    assert exc.value.message == "dbt deps failed: Runtime Error: no profile"


CONVENTION = "package:vecten_convention,test_type:singular"


def test_the_default_build_is_every_node_less_the_convention_tests():
    assert runner.BUILD_STEP == runner.build_args() == ("build", "--exclude", CONVENTION)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"select": "tag:intraday"}, ["--select", "tag:intraday", "--exclude", CONVENTION]),
        ({"select": "a  b\n"}, ["--select", "a", "b", "--exclude", CONVENTION]),
        ({"select": ["a", "b c"]}, ["--select", "a", "b", "c", "--exclude", CONVENTION]),
        ({"exclude": "marts"}, ["--exclude", CONVENTION, "marts"]),
        ({"full_refresh": True}, ["--exclude", CONVENTION, "--full-refresh"]),
        ({"select": "", "exclude": []}, ["--exclude", CONVENTION]),
    ],
)
def test_build_args_follow_the_run_parameters(kwargs, expected):
    assert runner.build_args(**kwargs) == ("build", *expected)


@pytest.mark.parametrize("value", ["--vars {}", "core -x", ["--full-refresh"]])
def test_a_selector_that_dbt_would_read_as_a_flag_is_refused(value):
    with pytest.raises(ValueError, match="does not start with '-'"):
        runner.build_args(select=value)
    with pytest.raises(ValueError, match="does not start with '-'"):
        runner.build_args(exclude=value)
