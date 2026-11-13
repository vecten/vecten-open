"""With the MCP, its state database alone grants `analyst` its reads: no setup text does."""

import re
from dataclasses import replace

import pytest

from vecten_shared.config import VectenConfig
from vecten_shared.identity_setup import provisioner_script
from vecten_shared.provider import snowflake_names
from vecten_shared.sql import setup_sql_path


def _snowflake_script(database="acme", warehouse="acme_wh", *, shared_account=False):
    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "acme", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "account": "org-account"},
        }
    )
    names = snowflake_names(config, "dev", shared_account=shared_account)
    if not shared_account:
        names = replace(names, database=database, warehouse=warehouse)
    return provisioner_script("snowflake", issuer="https://mcp.example", names=names)


def _statements(sql):
    """Each statement as one line, so a grant split over lines reads whole."""
    return [" ".join(part.split()) for part in sql.split(";") if part.strip()]


@pytest.mark.parametrize(
    ("shared_account", "analyst", "database", "warehouse"),
    [(False, "ANALYST", "ACME", "ACME_WH"), (True, "ANALYST_ACME_DEV", "ACME_DEV", "ACME_DEV_WH")],
)
def test_snowflake_script_grants_analyst_only_database_and_warehouse_usage(
    shared_account, analyst, database, warehouse
):
    sql = _snowflake_script(shared_account=shared_account)
    to_analyst = [
        s
        for s in _statements(sql)
        if s.startswith("GRANT") and re.search(rf"TO ROLE {analyst}\b", s)
    ]
    assert to_analyst == [
        f'GRANT USAGE ON DATABASE "{database}" TO ROLE {analyst}',
        f'GRANT USAGE ON WAREHOUSE "{warehouse}" TO ROLE {analyst}',
    ]


def test_snowflake_script_keeps_the_provisioners_grant_option_on_core_and_marts():
    sql = _snowflake_script(database="D", warehouse="W")
    statements = set(_statements(sql))
    for schema in ("CORE", "MARTS"):
        target = f'"D"."{schema}"'
        assert f"GRANT USAGE ON SCHEMA {target} TO ROLE PROVISIONER" in statements
        for shape in ("ALL", "FUTURE"):
            assert (
                f"GRANT SELECT ON {shape} TABLES IN SCHEMA {target} TO ROLE PROVISIONER "
                "WITH GRANT OPTION"
            ) in statements


def test_postgres_script_grants_analyst_only_connect():
    sql = provisioner_script("postgres", database="warehouse", issuer="https://mcp.example")
    grants = [line for line in sql.splitlines() if re.search(r"\banalyst\b", line)]
    privileges = [line for line in grants if re.search(r"(GRANT|PRIVILEGES).*TO analyst", line)]
    assert privileges == [
        'GRANT CONNECT ON DATABASE "warehouse" TO analyst, provisioner, impersonator;'
    ]
    assert "GRANT SELECT ON TABLES TO analyst" not in sql
    assert "GRANT USAGE ON SCHEMA %I TO analyst" not in sql


def _gated(sql, statement):
    """The `\\gexec` statement holding `statement`, whole."""
    start = sql.rindex("SELECT", 0, sql.index(statement))
    return sql[start : sql.index("\\gexec", start)]


def test_setup_sql_grants_analyst_its_reads_only_without_the_mcp():
    sql = setup_sql_path().read_text()
    reads = ["GRANT USAGE ON SCHEMA core, marts TO analyst"] + [
        f"ALTER DEFAULT PRIVILEGES FOR ROLE loader IN SCHEMA {schema} "
        "GRANT SELECT ON TABLES TO analyst"
        for schema in ("core", "marts")
    ]
    for statement in reads:
        assert sql.count(statement) == 1, statement
        assert "WHERE :'with_mcp' = 'false'" in _gated(sql, statement), statement
    others = [
        line
        for line in sql.splitlines()
        if re.search(r"GRANT .*\banalyst\b", line)
        and not line.startswith("--")
        and not any(s in line for s in reads)
    ]
    assert others == [
        "GRANT CONNECT ON DATABASE warehouse TO warehouse_owner, loader, analyst;",
        "SELECT 'GRANT analyst TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE' "
        "WHERE :'with_mcp' = 'true' \\gexec",
    ]
