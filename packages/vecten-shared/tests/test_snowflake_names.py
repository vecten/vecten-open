"""Every name Vecten gives an object in Snowflake, from one function."""

import ast
import re
from dataclasses import astuple
from pathlib import Path

import pytest

from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import SnowflakeNames, snowflake_names


def config(name, database=None, account="ACME-PROD", *, shared=False, environments=None):
    return VectenConfig.model_validate(
        {
            "users": 3,
            "project": {"name": name, "scaffold_version": "0.1.0"},
            "warehouse": {
                "kind": "snowflake",
                "account": account,
                "database": database,
                "shared_account": shared,
            },
            "environments": environments or {},
        }
    )


# The names running environments carry on release/0.1.1, spelled out so that no edit to the
# function can rename one: (project, [warehouse] database, env) -> (database, warehouse).
RELEASED = [
    ("acme", None, "prod", "ACME", "ACME_PROD_WH"),
    ("acme", None, "dev", "ACME", "ACME_DEV_WH"),
    ("acme", "analytics", "prod", "ANALYTICS", "ACME_PROD_WH"),
    ("acme-co", None, "staging", "ACME_CO", "ACME_CO_STAGING_WH"),
    ("acme-co", "analytics", "prod-2", "ANALYTICS", "ACME_CO_PROD_2_WH"),
    ("data_team", "Lake", "eu1", "LAKE", "DATA_TEAM_EU1_WH"),
]


@pytest.mark.parametrize(("project", "database", "env", "db_name", "wh_name"), RELEASED)
def test_fixed_names_are_the_released_literals(project, database, env, db_name, wh_name):
    names = snowflake_names(config(project, database), env)
    assert names.account == "ACME-PROD"
    assert names.suffix == ""
    assert names.database == db_name
    assert names.warehouse == wh_name
    assert names.analyst_role == "ANALYST"
    assert names.loader_role == "LOADER"
    assert names.loader_user == "LOADER"
    assert names.provisioner_role == "PROVISIONER"
    assert names.provisioner_user == "VECTEN_PROVISIONER"
    assert names.integration == "VECTEN_EXTERNAL_OAUTH"
    assert names.access_schema == "VECTEN_ACCESS"
    assert names.authentication_policy is None
    assert names.sign_in_schema is None and names.service_policy is None
    assert names.comment == f"vecten {project} {env} in ACME-PROD"
    assert names.audience == "https://ACME-PROD.snowflakecomputing.com"


def test_fixed_is_the_default_layout():
    assert snowflake_names(config("acme"), "prod") == snowflake_names(
        config("acme"), "prod", shared_account=False
    )


def test_names_in_a_shared_account_carry_the_environment_suffix():
    names = snowflake_names(config("acme"), "dev", shared_account=True)
    assert names == SnowflakeNames(
        account="ACME-PROD",
        suffix="_ACME_DEV",
        database="ACME_DEV",
        warehouse="ACME_DEV_WH",
        analyst_role="ANALYST_ACME_DEV",
        loader_role="LOADER_ACME_DEV",
        loader_user="LOADER_ACME_DEV",
        provisioner_role="PROVISIONER_ACME_DEV",
        provisioner_user="PROVISIONER_ACME_DEV",
        integration="EXTERNAL_OAUTH_ACME_DEV",
        access_schema="VECTEN_ACCESS",
        authentication_policy="MCP_SIGN_IN",
        comment="vecten acme dev in ACME-PROD",
        sign_in_schema="VECTEN_SIGN_IN",
        service_policy="SERVICE_SIGN_IN",
    )


def test_a_shared_account_bases_every_name_on_the_configured_database():
    names = snowflake_names(config("acme-co", "data-lake"), "prod", shared_account=True)
    assert names.suffix == "_DATA_LAKE_PROD"
    assert names.database == "DATA_LAKE_PROD"
    assert names.warehouse == "DATA_LAKE_PROD_WH"
    assert names.analyst_role == "ANALYST_DATA_LAKE_PROD"


def test_two_shared_environments_share_no_name():
    dev = snowflake_names(config("acme"), "dev", shared_account=True)
    prod = snowflake_names(config("acme"), "prod", shared_account=True)
    shared = set(astuple(dev)) & set(astuple(prod))
    assert shared == {
        "ACME-PROD",
        "VECTEN_ACCESS",
        "MCP_SIGN_IN",
        "VECTEN_SIGN_IN",
        "SERVICE_SIGN_IN",
    }


def test_an_undeployed_environment_takes_the_projects_setting():
    assert snowflake_names(config("acme", shared=True), "dev").suffix == "_ACME_DEV"
    assert snowflake_names(config("acme"), "dev").suffix == ""


@pytest.mark.parametrize("shared", [True, False])
def test_the_layout_an_environment_recorded_wins_over_the_setting(shared):
    recorded = {"prod": {"snowflake_shared_account": not shared}}
    names = snowflake_names(config("acme", shared=shared, environments=recorded), "prod")
    assert names.suffix == ("" if shared else "_ACME_PROD")


def test_an_environment_deployed_without_a_record_keeps_its_fixed_names():
    deployed = {"prod": {"deployed_commit": "a" * 40}}
    names = snowflake_names(config("acme", shared=True, environments=deployed), "prod")
    assert names.suffix == ""
    assert names.analyst_role == "ANALYST"


@pytest.mark.parametrize("project", ["acme", "acme-co"])
def test_a_shared_account_refuses_an_environment_name_with_a_hyphen(project):
    with pytest.raises(Failure) as caught:
        snowflake_names(config(project, shared=True), "dev-2")
    assert caught.value.code == ExitCode.CONFIG
    assert "hyphen" in caught.value.message
    assert "'dev2'" in caught.value.message


def test_a_fixed_environment_keeps_a_hyphen_in_its_warehouse_name():
    assert snowflake_names(config("acme"), "dev-2").warehouse == "ACME_DEV_2_WH"


def test_a_shared_account_refuses_a_suffix_over_64_characters():
    base = "d" * 59
    assert len(snowflake_names(config("acme", base, shared=True), "dev").suffix) == 64
    with pytest.raises(Failure) as caught:
        snowflake_names(config("acme", base + "x", shared=True), "dev")
    assert caught.value.code == ExitCode.CONFIG
    assert "_" + base.upper() + "X_DEV" in caught.value.message
    assert "64" in caught.value.message


@pytest.mark.parametrize("database", ["lake.raw", "lake raw", 'la"ke'])
def test_a_shared_account_refuses_a_base_that_is_no_plain_identifier(database):
    with pytest.raises(Failure) as caught:
        snowflake_names(config("acme", database, shared=True), "dev")
    assert "[warehouse] database" in caught.value.message


def test_the_environments_own_account_wins_over_vecten_toml():
    names = snowflake_names(config("acme"), "dev", account="ACME-DEV")
    assert names.account == "ACME-DEV"
    assert names.audience == "https://ACME-DEV.snowflakecomputing.com"
    assert snowflake_names(config("acme"), "dev", account=None).account == "ACME-PROD"


def test_the_audience_needs_an_account():
    names = snowflake_names(config("acme", account=None), "prod")
    assert names.account is None
    with pytest.raises(ValueError):
        _ = names.audience


REPO = Path(__file__).resolve().parents[3]
NAMING_MODULE = "packages/vecten-shared/src/vecten_shared/provider/__init__.py"
# A Snowflake role, user, integration, schema or policy name the function owns, or the
# warehouse suffix it builds; secret names such as LOADER_PASSWORD hold one only inside a
# longer word.
SPELLED = re.compile(
    r"(?<![A-Za-z0-9_])(ANALYST|LOADER|PROVISIONER|VECTEN_PROVISIONER|VECTEN_EXTERNAL_OAUTH"
    r"|VECTEN_ACCESS|MCP_SIGN_IN|VECTEN_SIGN_IN|SERVICE_SIGN_IN)(?![A-Za-z0-9_])|_WH(?![A-Za-z0-9_])"
)
# Constants a caller would keep beside the function, whatever their value.
CONSTANTS = re.compile(r"\bSNOWFLAKE_(LOADER|ANALYST|PROVISIONER)_(ROLE|USER)\b")
ALLOWED = {
    # The laptop's DuckDB emulation of a Snowflake connection, which reaches no account.
    "packages/vecten-platform/src/vecten_platform/dbt/local_warehouse.py",
    # The runbook's Snowflake section renders per layout from section 8 of the change.
    "packages/vecten/src/vecten/scaffold/template/docs/runbook.md.jinja",
}


def _docstrings(tree):
    kinds = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, kinds) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                yield id(first.value)


def spelled(path):
    """What `path` spells of the function's names: in Python, its strings other than
    docstrings and its constant names; in any other file, its whole text."""
    text = path.read_text(encoding="utf-8")
    found = [match.group(0) for match in CONSTANTS.finditer(text)]
    if path.suffix != ".py":
        return found + [match.group(0) for match in SPELLED.finditer(text)]
    tree = ast.parse(text)
    skip = set(_docstrings(tree))
    return found + [
        match.group(0)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
        for match in SPELLED.finditer(node.value)
    ]


def sources(root):
    return sorted(
        path
        for pattern in ("*.py", "*.js", "*.sql", "*.jinja")
        for path in (root / "packages").glob(f"*/src/**/{pattern}")
    )


def spelled_outside(root):
    return {
        rel: found
        for path in sources(root)
        if (rel := path.relative_to(root).as_posix()) not in ALLOWED | {NAMING_MODULE}
        if (found := spelled(path))
    }


def test_no_snowflake_name_is_spelled_outside_the_naming_function():
    assert len(sources(REPO)) > 100
    assert spelled_outside(REPO) == {}


def test_every_allowed_file_exists():
    assert all((REPO / rel).is_file() for rel in ALLOWED | {NAMING_MODULE})


@pytest.mark.parametrize(
    ("name", "text", "found"),
    [
        ("caller.py", 'ROLE = "LOADER"\n', ["LOADER"]),
        ("caller.py", "SNOWFLAKE_ANALYST_ROLE = role\n", ["SNOWFLAKE_ANALYST_ROLE"]),
        ("caller.py", 'warehouse = f"{project}_{env}_WH"\n', ["_WH"]),
        ("caller.py", 'sql = f"SHOW GRANTS TO ROLE PROVISIONER {x}"\n', ["PROVISIONER"]),
        ("grant.js", "if (TARGET !== 'ANALYST') {}\n", ["ANALYST"]),
        (
            "setup.sql",
            "CREATE SECURITY INTEGRATION VECTEN_EXTERNAL_OAUTH;\n",
            ["VECTEN_EXTERNAL_OAUTH"],
        ),
        ("caller.py", '"""Signs in as LOADER."""\nKEY = "LOADER_PASSWORD"\n', []),
    ],
)
def test_the_guard_catches_a_planted_name(tmp_path, name, text, found):
    package = tmp_path / "packages" / "vecten-mcp" / "src" / "vecten_mcp"
    package.mkdir(parents=True)
    (package / name).write_text(text)
    rel = f"packages/vecten-mcp/src/vecten_mcp/{name}"
    assert spelled_outside(tmp_path) == ({rel: found} if found else {})
