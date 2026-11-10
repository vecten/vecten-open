import json
import subprocess
import sys

from typer.core import TyperGroup
from typer.main import get_command

from vecten.cli.app import app
from vecten_shared.output import ExitCode
from vecten_shared.versions import VERSIONS


def iter_commands(cmd, prefix=""):
    if isinstance(cmd, TyperGroup):
        for name, sub in cmd.commands.items():
            yield from iter_commands(sub, prefix + name + " ")
    else:
        yield prefix.strip(), cmd


# The spec exempts `vecten dbt`: its arguments, output and exit code are dbt's own.
OUTSIDE_THE_JSON_CONTRACT = {"dbt"}


def test_every_registered_command_has_a_json_flag():
    names = []
    for name, cmd in iter_commands(get_command(app)):
        names.append(name)
        if name in OUTSIDE_THE_JSON_CONTRACT:
            continue
        assert any("--json" in p.opts for p in cmd.params), f"{name} lacks --json"
    assert "version" in names and "dbt-build-local" in names
    assert OUTSIDE_THE_JSON_CONTRACT <= set(names)


def test_version_json_is_one_document(invoke):
    result = invoke("version", "--json")
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True and doc["vecten"] == VERSIONS.vecten
    assert result.stdout.count("\n") == 1


def test_unknown_failure_classes_map_to_distinct_codes(invoke, monkeypatch):
    from vecten.cli.commands import version
    from vecten_shared.output import Failure

    def boom(ctx):
        raise Failure(ExitCode.TOOL_FAILED, "docker compose exited 1")

    monkeypatch.setattr(version, "run_version", boom)
    result = invoke("version", "--json")
    assert result.exit_code == ExitCode.TOOL_FAILED
    assert json.loads(result.stdout)["code_name"] == "TOOL_FAILED"
    assert result.stdout.count("\n") == 1


def test_internal_error_is_code_one_and_json(invoke, monkeypatch):
    from vecten.cli.commands import version

    def boom(ctx):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(version, "run_version", boom)
    result = invoke("version", "--json")
    assert result.exit_code == ExitCode.INTERNAL
    assert json.loads(result.stdout)["error"].startswith("internal error")
    assert result.stdout.count("\n") == 1


def test_failure_in_human_mode_goes_to_stderr_only(invoke, monkeypatch):
    from vecten.cli.commands import version
    from vecten_shared.output import Failure

    def boom(ctx):
        raise Failure(ExitCode.TOOL_FAILED, "docker compose exited 1")

    monkeypatch.setattr(version, "run_version", boom)
    result = invoke("version")
    assert result.exit_code == ExitCode.TOOL_FAILED
    assert result.stdout == ""
    assert result.stderr == "error: docker compose exited 1\n"


def test_importing_the_cli_does_not_import_dlt_dbt_fakesnow_boto3_or_psycopg():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, vecten.cli.app; "
            "print(sorted(m for m in ('dlt', 'dbt', 'fakesnow', 'boto3', 'botocore', 'psycopg') "
            "if m in sys.modules))",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_help_prints_nothing_on_stderr():
    proc = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""


def test_the_sync_runtime_imports_dlt_without_the_pkg_resources_warning():
    proc = subprocess.run(
        [sys.executable, "-W", "default", "-c", "import vecten_platform.sync.runner"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "pkg_resources" not in proc.stderr


def test_command_module_imports_cleanly_in_a_fresh_process():
    proc = subprocess.run(
        [sys.executable, "-c", "import vecten.cli.commands.version"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""
