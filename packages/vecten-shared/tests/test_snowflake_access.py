import json
import shutil
import subprocess

import pytest

from vecten_shared.identity_setup import provisioner_script


def test_metadata_procedure_returns_only_existence_and_binds_relation_identifiers():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the Snowflake JavaScript handler locally")
    sql = provisioner_script(
        "snowflake",
        database="D",
        warehouse="W",
        issuer="https://mcp.example",
        account="org-account",
    )
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
    sql = provisioner_script(
        "snowflake",
        database="D",
        warehouse="W",
        issuer="https://mcp.example",
        account="org-account",
    )
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
