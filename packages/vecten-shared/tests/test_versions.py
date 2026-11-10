import importlib
import re
import sys
import tomllib
from importlib.metadata import distributions
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from vecten_shared.versions import VERSIONS, minor_constraint, normalize_name, pins_for

REPO = Path(__file__).resolve().parents[3]


def assert_pins_agree(deps: list[str], source: str) -> None:
    for dep in deps:
        req = Requirement(dep)
        expected = pins_for(normalize_name(req.name))
        if expected is None:
            continue
        assert SpecifierSet(str(req.specifier)) == SpecifierSet(expected), (
            f"{source}: {dep} disagrees with versions.toml ({expected})"
        )


def _dependency_lists() -> list[tuple[list[str], str]]:
    lists = []
    for pyproject in sorted(REPO.glob("packages/*/pyproject.toml")):
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        lists.append((project.get("dependencies", []), f"{pyproject} dependencies"))
        for extra, deps in project.get("optional-dependencies", {}).items():
            lists.append((deps, f"{pyproject} optional-dependencies.{extra}"))
    root = REPO / "pyproject.toml"
    for group, deps in (
        tomllib.loads(root.read_text(encoding="utf-8")).get("dependency-groups", {}).items()
    ):
        lists.append(([d for d in deps if isinstance(d, str)], f"{root} dependency-groups.{group}"))
    return lists


def test_table_has_every_governed_entry():
    for name in (
        "vecten",
        "python",
        "airflow_image",
        "postgres_major",
        "nginx_image",
        "log_cleaner_image",
        "mcp_base_image",
        "dbt_core",
        "dbt_postgres",
        "dbt_snowflake",
        "fakesnow",
        "dlt",
        "mcp_sdk",
        "sqlglot",
        "ossie_spec",
        "terraform",
        "terraform_module_ref",
        "vecten_convention",
        "airflow_mcp_server",
        "dbt_mcp",
        "boto3",
        "psycopg",
        "schema_convention",
        "certbot_image",
        "terraform_aws_provider",
        "terraform_snowflake_provider",
        "terraform_scaleway_provider",
        "terraform_random_provider",
        "docker_compose_plugin",
        "docker_buildx_plugin",
    ):
        assert getattr(VERSIONS, name), name


def test_airflow_version_from_image_tag():
    assert VERSIONS.airflow_version == "3.3.2"


def test_terraform_provider_pins_are_minors():
    for pin in (
        VERSIONS.terraform_aws_provider,
        VERSIONS.terraform_snowflake_provider,
        VERSIONS.terraform_scaleway_provider,
        VERSIONS.terraform_random_provider,
    ):
        assert re.fullmatch(r"\d+\.\d+", pin), pin


def test_scaleway_module_provider_pins():
    assert VERSIONS.terraform_scaleway_provider == "2.87"
    assert VERSIONS.terraform_random_provider == "3.9"


def test_vm_tool_pins_are_exact():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSIONS.docker_compose_plugin)
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSIONS.docker_buildx_plugin)
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSIONS.ruff)
    assert re.fullmatch(r"certbot/certbot:v\d+\.\d+\.\d+", VERSIONS.certbot_image)


def test_minor_constraint_shape():
    assert minor_constraint("1.12.3") == ">=1.12,<1.13"
    assert minor_constraint("0.47") == ">=0.47,<0.48"


def test_pins_for_normalizes_names():
    for name in ("dbt-core", "dbt_core", "DBT-Core", "dbt.core"):
        assert pins_for(name) == minor_constraint(VERSIONS.dbt_core), name
    assert pins_for("pydantic") is None


def test_dbt_adapter_and_emulation_pins():
    assert pins_for("dbt-postgres") == ">=1.11,<1.12"
    assert pins_for("dbt_snowflake") == ">=1.12,<1.13"
    assert pins_for("fakesnow") == ">=0.11,<0.12"
    assert pins_for("sqlglot") == ">=30.22,<30.23"


def test_boto3_pin_governs_the_shared_package():
    assert pins_for("boto3") == minor_constraint(VERSIONS.boto3)
    shared = tomllib.loads((REPO / "packages/vecten-shared/pyproject.toml").read_text())
    assert any(Requirement(d).name == "boto3" for d in shared["project"]["dependencies"])


def test_psycopg_pin_governs_the_cli_package():
    assert pins_for("psycopg") == minor_constraint(VERSIONS.psycopg)
    cli = tomllib.loads((REPO / "packages/vecten/pyproject.toml").read_text())
    assert any(Requirement(d).name == "psycopg" for d in cli["project"]["dependencies"])


def test_every_pyproject_agrees_with_the_table():
    for deps, source in _dependency_lists():
        assert_pins_agree(deps, source)


def test_guard_rejects_a_drifted_pin():
    with pytest.raises(AssertionError, match=r"dbt_core>=1\.12,<1\.70"):
        assert_pins_agree(["dbt_core>=1.12,<1.70"], "synthetic")
    assert_pins_agree(["dbt-core>=1.12,<1.13"], "synthetic")


def test_vecten_version_matches_distributions():
    for dist in ("vecten", "vecten-platform", "vecten-mcp", "vecten-shared"):
        pyproject = REPO / "packages" / dist / "pyproject.toml"
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        assert project["version"] == VERSIONS.vecten, dist
        module = importlib.import_module(dist.replace("-", "_"))
        assert module.__version__ == VERSIONS.vecten, dist


def test_every_python_declaration_names_the_table_python():
    floor = SpecifierSet(f">={VERSIONS.python}")
    for pyproject in [REPO / "pyproject.toml", *sorted(REPO.glob("packages/*/pyproject.toml"))]:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        assert SpecifierSet(project["requires-python"]) == floor, pyproject
    assert (REPO / ".python-version").read_text().strip() == VERSIONS.python


def test_every_installed_distribution_admits_the_table_python():
    # uv ignores upper bounds on a dependency's Requires-Python when it locks, while pip in the
    # runtime images enforces them, so a bound that excludes the table's Python fails only there.
    running = ".".join(map(str, sys.version_info[:3]))
    assert running.startswith(f"{VERSIONS.python}."), running
    excluded = sorted(
        f"{dist.metadata['Name']} {dist.version} ({bound})"
        for dist in distributions()
        if (bound := dist.metadata.get("Requires-Python")) and running not in SpecifierSet(bound)
    )
    assert not excluded, excluded


def test_github_actions_are_pinned_to_release_commits():
    # A tag can be moved to other code; a commit cannot, and the comment names the release.
    assert VERSIONS.github_runner == "ubuntu-24.04"
    assert set(VERSIONS.github_actions) == {
        "checkout",
        "setup_uv",
        "setup_terraform",
        "configure_aws_credentials",
    }
    for name, uses in VERSIONS.github_actions.items():
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+", uses), name


def test_repository_ci_uses_the_table_actions_and_runner():
    lines = (REPO / ".github" / "workflows" / "ci.yml").read_text().splitlines()
    uses = [line.split("uses:", 1)[1].strip() for line in lines if "uses:" in line]
    runners = [line.split("runs-on:", 1)[1].strip() for line in lines if "runs-on:" in line]
    assert uses and runners
    assert set(uses) <= set(VERSIONS.github_actions.values())
    assert set(runners) == {VERSIONS.github_runner}
