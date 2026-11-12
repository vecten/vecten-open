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


def read_grant_body(sql):
    """READ_GRANT's handler: the first procedure body the script creates."""
    return sql.split("EXECUTE AS OWNER AS $$", 1)[1].split("$$", 1)[0]


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
    handler = read_grant_body(sql)
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
    assert "CREATE ROLE IF NOT EXISTS ANALYST_ACME_DEV COMMENT" in sql
    assert "CREATE ROLE IF NOT EXISTS PROVISIONER_ACME_DEV COMMENT" in sql
    assert "CREATE USER IF NOT EXISTS PROVISIONER_ACME_DEV TYPE = SERVICE COMMENT" in sql
    assert "GRANT ROLE PROVISIONER_ACME_DEV TO USER PROVISIONER_ACME_DEV;" in sql
    assert "CREATE SECURITY INTEGRATION IF NOT EXISTS EXTERNAL_OAUTH_ACME_DEV" in sql
    assert 'CREATE SCHEMA IF NOT EXISTS "ACME_DEV"."VECTEN_ACCESS" COMMENT' in sql
    assert 'GRANT USAGE ON WAREHOUSE "ACME_DEV_WH" TO ROLE ANALYST_ACME_DEV;' in sql
    assert "('https://org-account.snowflakecomputing.com')" in sql
    handler = read_grant_body(sql)
    for value in ("ANALYST_ACME_DEV", "PROVISIONER_ACME_DEV", "LOADER_ACME_DEV", "_ACME_DEV"):
        assert json.dumps(value) in handler, value


@pytest.mark.parametrize("role", ["P; DROP DATABASE D", "_PROVISIONER", "provisioner"])
def test_a_name_that_is_no_plain_identifier_is_refused(role):
    # The same rule as the Snowflake module's role variables: ^[A-Z][A-Z0-9_]{0,254}$.
    with pytest.raises(ValueError):
        provisioner_script(
            "snowflake",
            issuer="https://mcp.example",
            names=replace(names(), provisioner_role=role),
        )


COMMENT = "'vecten acme dev in org-account'"


def shared_script():
    return provisioner_script(
        "snowflake", issuer="https://mcp.dev.example", names=names(shared_account=True, env="dev")
    )


def test_every_object_of_the_shared_script_carries_the_environments_comment():
    sql = shared_script()
    for statement in (
        f"CREATE ROLE IF NOT EXISTS ANALYST_ACME_DEV COMMENT = {COMMENT};",
        f"CREATE ROLE IF NOT EXISTS PROVISIONER_ACME_DEV COMMENT = {COMMENT};",
        f"CREATE USER IF NOT EXISTS PROVISIONER_ACME_DEV TYPE = SERVICE COMMENT = {COMMENT};",
        f'CREATE SCHEMA IF NOT EXISTS "ACME_DEV"."VECTEN_ACCESS" COMMENT = {COMMENT};',
    ):
        assert statement in sql, statement
    integration = sql.split("CREATE SECURITY INTEGRATION IF NOT EXISTS EXTERNAL_OAUTH_ACME_DEV")[1]
    assert f"COMMENT = {COMMENT};" in integration.split(";")[0] + ";"


def test_the_shared_script_binds_sign_in_to_the_environments_integration():
    sql = shared_script()
    policy = '"ACME_DEV"."VECTEN_ACCESS"."MCP_SIGN_IN"'
    assert (
        f"CREATE AUTHENTICATION POLICY IF NOT EXISTS {policy}\n"
        "  AUTHENTICATION_METHODS = ('OAUTH')\n"
        "  SECURITY_INTEGRATIONS = ('EXTERNAL_OAUTH_ACME_DEV')\n"
        f"  COMMENT = {COMMENT};"
    ) in sql
    assert f"GRANT APPLY ON AUTHENTICATION POLICY {policy} TO ROLE PROVISIONER_ACME_DEV;" in sql
    # The policy names the integration, which must exist first.
    assert sql.index("CREATE SECURITY INTEGRATION") < sql.index("CREATE AUTHENTICATION POLICY")


def test_the_shared_script_stops_before_any_change_at_an_object_with_another_comment():
    sql = shared_script()
    guard, rest = sql.split("CREATE ROLE IF NOT EXISTS", 1)
    assert "CREATE" not in guard.replace("CREATE ROLE IF NOT EXISTS", "")
    assert guard.count("EXECUTE IMMEDIATE $$") == 1
    for show, name in (
        ("ROLES", "ANALYST_ACME_DEV"),
        ("ROLES", "PROVISIONER_ACME_DEV"),
        ("USERS", "PROVISIONER_ACME_DEV"),
        ("SECURITY INTEGRATIONS", "EXTERNAL_OAUTH_ACME_DEV"),
    ):
        assert f"SHOW {show} LIKE '{name}';" in guard, name
        # LIKE treats `_` as a wildcard, so the exact name is compared again.
        assert f"WHERE \"name\" = '{name}' AND COALESCE(\"comment\", '') <> {COMMENT};" in guard
        assert f"{name} exists" in guard
    assert "RAISE" in guard


def test_the_fixed_script_keeps_its_statements():
    sql = provisioner_script("snowflake", issuer="https://mcp.example", names=names())
    for absent in ("COMMENT", "AUTHENTICATION POLICY", "EXECUTE IMMEDIATE", "APPLY"):
        assert absent not in sql, absent


def _read_grant(sql, target, owner, schema="CORE"):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the Snowflake JavaScript handler locally")
    harness = r"""
const fs = require('fs');
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
const statements = [];
const snowflake = {execute: ({sqlText}) => {
    statements.push(sqlText);
    if (sqlText.startsWith('SHOW GRANTS')) {
        let first = true;
        return {next: () => {const value = first; first = false; return value;},
                getColumnValue: (key) => ({privilege:'OWNERSHIP',
                    grantee_name:p.owner, granted_to:'ROLE'})[key]};
    }
    return {};
}};
let error = null;
try { new Function('snowflake','TARGET','SCHEMA_NAME','RELATION_NAME','ENABLED',p.handler)(
    snowflake,p.target,p.schema,undefined,1);
} catch (e) { error = String(e); }
process.stdout.write(JSON.stringify({statements,error}));
"""
    handler = read_grant_body(sql)
    result = subprocess.run(
        [node, "-e", harness],
        input=json.dumps(dict(handler=handler, target=target, owner=owner, schema=schema)),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("FINANCE_ACME_DEV", None),
        ("ANALYST_ACME_DEV", None),
        ("F" * 63 + "_ACME_DEV", None),
        ("FINANCE_ACME_PROD", "not a role of this environment"),
        ("FINANCE", "not a role of this environment"),
        ("_ACME_DEV", "bounded reader role"),
        ("F" * 64 + "_ACME_DEV", "bounded reader role"),
    ],
)
def test_read_grant_refuses_another_environments_role_before_its_ownership(target, error):
    output = _read_grant(shared_script(), target, owner="PROVISIONER_ACME_DEV")
    if error is None:
        assert output["error"] is None, output
        assert any(s.startswith("GRANT SELECT") for s in output["statements"])
    else:
        assert error in output["error"]
        assert output["statements"] == []


def test_read_grant_keeps_ownership_as_the_guard_behind_the_suffix():
    # `_X_ACME_DEV` ends in `_ACME_DEV`: another project's role passes the suffix filter only.
    output = _read_grant(shared_script(), "FINANCE_X_ACME_DEV", owner="PROVISIONER_X_ACME_DEV")
    assert "not owned by provisioner" in output["error"]
    assert not any(s.startswith("GRANT") for s in output["statements"])


@pytest.mark.parametrize(("length", "refused"), [(63, False), (64, True)])
def test_read_grant_bounds_a_fixed_target_as_before(length, refused):
    sql = provisioner_script("snowflake", issuer="https://mcp.example", names=names())
    output = _read_grant(sql, "F" * length, owner="PROVISIONER")
    assert (output["error"] is not None) is refused, output
