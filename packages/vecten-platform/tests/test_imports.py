import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parents[3]


def test_dlt_imports_without_warnings_becoming_errors():
    # The pytest configuration turns warnings into errors; dlt must import under it.
    proc = subprocess.run(
        [sys.executable, "-W", "error", "-c", "import dlt"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_platform_depends_on_dlt_with_every_extra_the_runtime_needs():
    pyproject = tomllib.loads((REPO / "packages/vecten-platform/pyproject.toml").read_text())
    reqs = {Requirement(r).name: Requirement(r) for r in pyproject["project"]["dependencies"]}
    assert reqs["dlt"].extras == {"duckdb", "postgres", "snowflake", "sql_database", "filesystem"}


def test_vecten_depends_on_platform():
    pyproject = tomllib.loads((REPO / "packages/vecten/pyproject.toml").read_text())
    assert "vecten-platform" in {Requirement(r).name for r in pyproject["project"]["dependencies"]}


def test_root_pytest_config_turns_every_warning_into_an_error():
    root = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert root["tool"]["pytest"]["ini_options"]["filterwarnings"] == ["error"]
    assert "network: reaches GitHub or PyPI" in root["tool"]["pytest"]["ini_options"]["markers"]


def test_platform_and_dbt_packages_import_without_heavy_runtimes():
    # The CLI imports these on every command; dbt, dlt, fakesnow and Airflow load lazily.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, vecten_platform, vecten_platform.dbt; "
            "print(sorted(m for m in ('dbt', 'dlt', 'fakesnow', 'airflow') if m in sys.modules))",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"
