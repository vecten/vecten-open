import json
import re
import shutil
import subprocess
from dataclasses import replace

import pytest

from vecten_shared.config import VectenConfig
from vecten_shared.identity_setup import provisioner_script
from vecten_shared.provider import snowflake_names


def names(database="D", warehouse="W", *, shared_account=False, env="prod"):
    """The names of project `acme`, over a test database and warehouse in the fixed layout."""
    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "acme", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "account": "org-account"},
        }
    )
    derived = snowflake_names(config, env, shared_account=shared_account)
    if shared_account:
        return derived
    return replace(derived, database=database, warehouse=warehouse)


def test_metadata_procedure_returns_only_existence_and_binds_relation_identifiers():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the Snowflake JavaScript handler locally")
    sql = provisioner_script("snowflake", issuer="https://mcp.example", names=names())
    handler = sql.split("$$")[3]
    harness = r"""
const fs = require('fs');
const handler = JSON.parse(fs.readFileSync(0, 'utf8'));
let statement;
const snowflake = {execute: s => {statement=s; return {next:()=>true,getColumnValue:()=>1}}};
const value = new Function('snowflake','SCHEMA_NAME','RELATION_NAME',handler)(
    snowflake, 'RAW_APP', 'Orders\";DROP TABLE x;--');
process.stdout.write(JSON.stringify({value,statement}));
"""
    result = subprocess.run(
        [node, "-e", harness], input=json.dumps(handler), text=True, capture_output=True, check=True
    )
    output = json.loads(result.stdout)
    assert output["value"] is True
    assert output["statement"]["sqlText"] == (
        'SELECT COUNT(*) FROM "D".INFORMATION_SCHEMA.TABLES '
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
    )
    assert output["statement"]["binds"] == ["RAW_APP", 'Orders";DROP TABLE x;--']


@pytest.mark.parametrize(
    ("target", "schema", "relation", "enabled", "owned", "error"),
    [
        ("FINANCE", "STAGING", None, True, True, False),
        ("FINANCE", "STAGING", None, False, True, False),
        ("ANALYST", "CORE", "ORDERS", True, False, False),
        ("FINANCE", "CORE", "ORDERS", False, True, False),
        ("SECURITYADMIN", "CORE", None, True, False, True),
        ("PROVISIONER", "CORE", None, True, True, True),
        ("FINANCE", "PRIVATE", None, True, True, True),
        ("FINANCE", "RAW_CRM", None, True, False, True),
        ("FINANCE", "RAW_CRM", 'x";DROP DATABASE D;--', True, True, False),
        ("FINANCE", "CORE", None, None, True, True),
    ],
)
def test_owner_procedure_enforces_boundaries(target, schema, relation, enabled, owned, error):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the Snowflake JavaScript handler locally")
    sql = provisioner_script("snowflake", issuer="https://mcp.example", names=names())
    assert "EXECUTE AS OWNER" in sql
    handler = sql.split("$$", 2)[1]
    harness = r"""
const fs = require('fs');
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
const statements = [];
const snowflake = {execute: ({sqlText, binds}) => {
    if (sqlText.startsWith('SHOW GRANTS')) {
        let first = true;
        return {next: () => {const value = first && p.owned; first = false; return value;},
                getColumnValue: (key) => ({privilege:'OWNERSHIP',
                    grantee_name:'PROVISIONER', granted_to:'ROLE'})[key]};
    }
    if (sqlText.startsWith('SELECT TABLE_TYPE')) {
        return {next: () => true, getColumnValue: () => 'BASE TABLE'};
    }
    statements.push(sqlText);
    return {};
}};
// Snowflake hands a JavaScript procedure a BOOLEAN argument as a number and SQL NULL as undefined.
const arg = v => v === null ? undefined : (typeof v === 'boolean' ? Number(v) : v);
let error = null;
try { new Function('snowflake','TARGET','SCHEMA_NAME','RELATION_NAME','ENABLED',p.handler)(
    snowflake,arg(p.target),arg(p.schema),arg(p.relation),arg(p.enabled));
} catch (e) { error = String(e); }
process.stdout.write(JSON.stringify({statements,error}));
"""
    result = subprocess.run(
        [node, "-e", harness],
        input=json.dumps(
            dict(
                handler=handler,
                target=target,
                schema=schema,
                relation=relation,
                enabled=enabled,
                owned=owned,
            )
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    output = json.loads(result.stdout)
    assert bool(output["error"]) is error, output
    if error:
        assert output["statements"] == []
    elif relation is None:
        verb = "GRANT" if enabled else "REVOKE"
        for kind in ("TABLES", "VIEWS"):
            assert any(f"{verb} SELECT ON FUTURE {kind}" in s for s in output["statements"])
            assert any(f"{verb} SELECT ON ALL {kind}" in s for s in output["statements"])
    else:
        assert not any("ALL TABLES" in s or "FUTURE" in s for s in output["statements"])
        assert sum("SELECT ON TABLE" in s for s in output["statements"]) == 1
        if not enabled:
            assert not any("USAGE" in s for s in output["statements"])


FIXED_NAMES = re.compile(
    r"\b(ANALYST|LOADER|PROVISIONER|VECTEN_PROVISIONER|VECTEN_EXTERNAL_OAUTH)\b"
)


def test_the_script_takes_every_name_from_the_snowflake_names():
    shared = names(shared_account=True, env="dev")
    sql = provisioner_script("snowflake", issuer="https://mcp.example", names=shared)
    assert not FIXED_NAMES.findall(sql)
    assert "CREATE ROLE IF NOT EXISTS ANALYST_ACME_DEV;" in sql
    assert "CREATE ROLE IF NOT EXISTS PROVISIONER_ACME_DEV;" in sql
    assert "CREATE USER IF NOT EXISTS PROVISIONER_ACME_DEV TYPE = SERVICE;" in sql
    assert "GRANT ROLE PROVISIONER_ACME_DEV TO USER PROVISIONER_ACME_DEV;" in sql
    assert "CREATE SECURITY INTEGRATION IF NOT EXISTS EXTERNAL_OAUTH_ACME_DEV" in sql
    assert 'CREATE SCHEMA IF NOT EXISTS "ACME_DEV"."VECTEN_ACCESS";' in sql
    assert 'GRANT USAGE ON WAREHOUSE "ACME_DEV_WH" TO ROLE ANALYST_ACME_DEV;' in sql
    assert "('https://org-account.snowflakecomputing.com')" in sql
    handler = sql.split("$$", 2)[1]
    for value in ("ANALYST_ACME_DEV", "PROVISIONER_ACME_DEV", "LOADER_ACME_DEV", "_ACME_DEV"):
        assert json.dumps(value) in handler, value


def test_a_name_that_is_no_plain_identifier_is_refused():
    with pytest.raises(ValueError):
        provisioner_script(
            "snowflake",
            issuer="https://mcp.example",
            names=replace(names(), provisioner_role="P; DROP DATABASE D"),
        )
