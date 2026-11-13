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
    """READ_GRANT's handler: the first procedure body the script creates, `$$`-quoted with
    fixed names and a single-quoted literal inside the shared script's block."""
    if "EXECUTE AS OWNER AS $$" in sql:
        return sql.split("EXECUTE AS OWNER AS $$", 1)[1].split("$$", 1)[0]
    literal = sql.split("EXECUTE AS OWNER AS '", 1)[1]
    end = re.search(r"(?<!')'(?:'')*;", literal)
    body = literal[: end.start() + len(end.group(0)) - 2]
    return body.replace("''", "'").replace("\\\\", "\\")


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


KINDS = (
    "TABLES",
    "VIEWS",
    "MATERIALIZED VIEWS",
    "DYNAMIC TABLES",
    "EXTERNAL TABLES",
    "ICEBERG TABLES",
)


@pytest.mark.parametrize(
    ("target", "schema", "relation", "enabled", "owned", "error"),
    [
        ("FINANCE", "STAGING", None, True, True, False),
        ("FINANCE", "STAGING", None, False, True, False),
        ("ANALYST", "CORE", "ORDERS", True, False, False),
        ("FINANCE", "CORE", "ORDERS", False, True, False),
        ("SECURITYADMIN", "CORE", None, True, False, True),
        ("PROVISIONER", "CORE", None, True, True, True),
        ("READER", "CORE", None, True, True, True),
        ("FINANCE", "PRIVATE", None, True, True, True),
        ("FINANCE", "RAW_CRM", None, True, False, True),
        ("FINANCE", "RAW_CRM", 'x";DROP DATABASE D;--', True, True, False),
        ("FINANCE", "CORE", None, None, True, True),
        ("FINANCE", "MARTS_FINANCE", None, True, True, False),
        ("FINANCE", "CORE_FINANCE", "INVOICE", True, True, False),
        ("FINANCE", "CORE_FINANCE", None, False, True, False),
        ("FINANCE", "MARTS_FIN_OPS", None, True, True, True),
        ("FINANCE", "MARTS_", None, True, True, True),
        ("FINANCE", "CORE_2024", None, True, True, True),
        ("FINANCE", "STAGING_FINANCE", None, True, True, True),
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
// The relation's SELECT as SHOW GRANTS TO ROLE reads it back: held before a revoke.
let selects = p.enabled === false && p.relation !== null;
const snowflake = {execute: ({sqlText, binds}) => {
    if (sqlText.startsWith('SHOW GRANTS ON ROLE')) {
        let first = true;
        return {next: () => {const value = first && p.owned; first = false; return value;},
                getColumnValue: (key) => ({privilege:'OWNERSHIP',
                    grantee_name:'PROVISIONER', granted_to:'ROLE'})[key]};
    }
    if (sqlText.startsWith('SHOW GRANTS TO ROLE')) {
        let first = true;
        const name = 'D.' + p.schema + '.' + p.relation;
        return {next: () => {const value = first && selects; first = false; return value;},
                getColumnValue: (key) => ({privilege:'SELECT', name})[key]};
    }
    if (sqlText.startsWith('SELECT TABLE_TYPE')) {
        return {next: () => true, getColumnValue: () => 'BASE TABLE'};
    }
    if (/^(GRANT|REVOKE) SELECT ON TABLE /.test(sqlText)) selects = sqlText.startsWith('GRANT');
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
        for kind in KINDS:
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
        "    AUTHENTICATION_METHODS = ('OAUTH')\n"
        "    SECURITY_INTEGRATIONS = ('EXTERNAL_OAUTH_ACME_DEV')\n"
        f"    COMMENT = {COMMENT};"
    ) in sql
    assert f"GRANT APPLY ON AUTHENTICATION POLICY {policy} TO ROLE PROVISIONER_ACME_DEV;" in sql
    # The policy names the integration, which must exist first.
    assert sql.index("CREATE SECURITY INTEGRATION") < sql.index("CREATE AUTHENTICATION POLICY")


def _block_and_reports(sql):
    """The shared script's one block, and what follows it."""
    head, rest = sql.split("EXECUTE IMMEDIATE $$\n", 1)
    block, reports = rest.split("\n$$;\n", 1)
    return head, block, reports


def test_every_change_of_the_shared_script_runs_in_one_block_that_stops_at_the_first_error():
    head, block, reports = _block_and_reports(shared_script())
    assert all(line.startswith("--") for line in head.splitlines())
    assert "$$" not in block
    assert block.rstrip().endswith("END;")
    # After the block only reports run: SHOW and SELECT print what an administrator drops.
    changing = re.compile(r"\s*(CREATE|GRANT|ALTER|DROP|REVOKE|EXECUTE|CALL|USE)\b")
    assert not [line for line in reports.splitlines() if changing.match(line)]
    assert "SHOW GRANTS TO ROLE PROVISIONER_ACME_DEV;" in reports
    for statement in (
        "CREATE ROLE",
        "GRANT CREATE USER",
        "CREATE SECURITY INTEGRATION",
        "CREATE AUTHENTICATION POLICY",
        "CREATE OR REPLACE PROCEDURE",
    ):
        assert statement in block, statement
    # The checks come before the first change.
    assert block.index("RAISE taken_4") < block.index("CREATE ROLE")


def test_the_shared_script_stops_at_an_object_another_project_owns_or_comments():
    _, block, _ = _block_and_reports(shared_script())
    assert "SHOW DATABASES LIKE 'ACME_DEV';" in block
    assert 'SELECT MAX("owner") INTO :keeper' in block
    for show, kind, name in (
        ("ROLES", "ROLE", "ANALYST_ACME_DEV"),
        ("ROLES", "ROLE", "PROVISIONER_ACME_DEV"),
        ("USERS", "USER", "PROVISIONER_ACME_DEV"),
        ("SECURITY INTEGRATIONS", "INTEGRATION", "EXTERNAL_OAUTH_ACME_DEV"),
    ):
        assert f"SHOW {show} LIKE '{name}';" in block, name
        # LIKE treats `_` as a wildcard, so the exact name is compared again.
        assert f"WHERE \"name\" = '{name}';" in block, name
        assert f'SHOW GRANTS ON {kind} "{name}";' in block, name
        assert f"{name} exists" in block
    assert f"COUNT_IF(COALESCE(\"comment\", '') <> {COMMENT})" in block
    assert '"grantee_name" IN (CURRENT_ROLE(), :keeper)' in block
    assert "IF (foreign > 0 OR owners = 0) THEN RAISE" in block


def test_the_shared_provisioner_user_signs_in_by_its_key_alone():
    _, block, _ = _block_and_reports(shared_script())
    policy = '"ACME_DEV"."VECTEN_ACCESS"."SERVICE_SIGN_IN"'
    assert "ALTER USER PROVISIONER_ACME_DEV UNSET RSA_PUBLIC_KEY_2;" in block
    assert "ALTER USER PROVISIONER_ACME_DEV UNSET PASSWORD;" in block
    assert (
        f"CREATE AUTHENTICATION POLICY IF NOT EXISTS {policy}\n"
        "    AUTHENTICATION_METHODS = ('KEYPAIR')\n"
        f"    COMMENT = {COMMENT};"
    ) in block
    assert f"ALTER USER PROVISIONER_ACME_DEV SET AUTHENTICATION POLICY {policy};" in block
    assert block.index("UNSET AUTHENTICATION POLICY") < block.index(
        f"SET AUTHENTICATION POLICY {policy}"
    )


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


def _script():
    return provisioner_script("snowflake", issuer="https://mcp.example", names=names())


RUN = r"""
const fs = require('fs');
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
const statements = [];
const rows = (list) => {
    let i = -1;
    return {next: () => ++i < list.length, getColumnValue: (key) => list[i][key]};
};
const snowflake = {execute: ({sqlText}) => {
    statements.push(sqlText);
    if (sqlText.startsWith('SHOW GRANTS ON ROLE')) {
        return rows(p.owned ? [{privilege:'OWNERSHIP', granted_to:'ROLE',
                                grantee_name:p.owner || 'PROVISIONER'}] : []);
    }
    if (sqlText.startsWith('SHOW GRANTS TO ROLE')) return rows(p.grants || []);
    if (sqlText.startsWith('SHOW FUTURE GRANTS TO ROLE')) return rows(p.future || []);
    return rows([]);
}};
const arg = v => v === null ? undefined : (typeof v === 'boolean' ? Number(v) : v);
let value = null, error = null;
try { value = new Function('snowflake', ...p.names, p.handler)(snowflake, ...p.args.map(arg)); }
catch (e) { error = String(e); }
process.stdout.write(JSON.stringify({value, error, statements}));
"""


def _run(handler, names, args, **fixture):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the Snowflake JavaScript handler locally")
    result = subprocess.run(
        [node, "-e", RUN],
        input=json.dumps(dict(handler=handler, names=names, args=args, **fixture)),
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


def test_the_shared_script_stops_at_an_environment_role_held_by_a_stranger():
    _, block, _ = _block_and_reports(shared_script())
    checks = block[: block.index("CREATE ROLE")]
    # The provisioner's role: its own user, and the roles the Snowflake root grants it to.
    assert 'SHOW GRANTS OF ROLE "PROVISIONER_ACME_DEV";' in checks
    assert "\"granted_to\" = 'USER' AND \"grantee_name\" = 'PROVISIONER_ACME_DEV'" in checks
    assert "\"granted_to\" = 'ROLE' AND \"grantee_name\" IN ('SYSADMIN', :keeper)" in checks
    # The analyst role: also the provisioner's role and whatever the provisioner owns.
    assert 'SHOW GRANTS OF ROLE "ANALYST_ACME_DEV";' in checks
    assert "IN ('PROVISIONER_ACME_DEV', 'SYSADMIN', :keeper)" in checks
    assert 'SHOW GRANTS TO ROLE "PROVISIONER_ACME_DEV";' in checks
    assert "owned := LAST_QUERY_ID();" in checks
    assert "TABLE(RESULT_SCAN(:owned))" in checks
    assert "PROVISIONER_ACME_DEV is held by" in checks and "ANALYST_ACME_DEV is held by" in checks


def test_the_shared_script_stops_at_an_access_schema_or_policy_it_does_not_own():
    _, block, _ = _block_and_reports(shared_script())
    checks = block[: block.index("CREATE ROLE")]
    assert "SHOW SCHEMAS LIKE 'VECTEN_ACCESS' IN DATABASE \"ACME_DEV\";" in checks
    for policy in ("MCP_SIGN_IN", "SERVICE_SIGN_IN"):
        assert (
            f'SHOW AUTHENTICATION POLICIES LIKE \'{policy}\' IN SCHEMA "ACME_DEV"."VECTEN_ACCESS";'
            in checks
        ), policy
        assert f"{policy} is owned by another role" in checks
    assert '"owner" <> CURRENT_ROLE()' in checks


def test_each_shared_integration_refuses_the_deploy_role_and_sysadmin():
    _, block, _ = _block_and_reports(shared_script())
    assert "SET EXTERNAL_OAUTH_BLOCKED_ROLES_LIST = '" in block
    assert "(''SYSADMIN'', ''' || REPLACE(COALESCE(keeper, 'SYSADMIN')" in block
    assert "EXECUTE IMMEDIATE :blocked;" in block
    assert block.index("CREATE SECURITY INTEGRATION") < block.index("EXECUTE IMMEDIATE :blocked;")


def test_a_dollar_quote_inside_the_block_refuses_the_script(monkeypatch):
    import vecten_shared.identity_setup as setup

    monkeypatch.setattr(setup, "_single_quoted_bodies", lambda text: text)
    with pytest.raises(ValueError, match=r"\$\$"):
        shared_script()


def test_read_grant_with_every_argument_null_reports_its_version_and_changes_nothing():
    from vecten_shared.identity_setup import ACCESS_VERSION

    handler = _script().split("$$")[1]
    names = ["TARGET", "SCHEMA_NAME", "RELATION_NAME", "ENABLED"]
    output = _run(handler, names, [None, None, None, None])
    assert output == {"value": f"vecten-access {ACCESS_VERSION}", "error": None, "statements": []}


def test_script_installs_read_grants_for_the_provisioner_to_call():
    sql = _script()
    assert 'CREATE OR REPLACE PROCEDURE "D"."VECTEN_ACCESS".READ_GRANTS(TARGET VARCHAR)' in sql
    assert "RETURNS VARIANT LANGUAGE JAVASCRIPT EXECUTE AS OWNER" in sql
    assert (
        'GRANT USAGE ON PROCEDURE "D"."VECTEN_ACCESS".READ_GRANTS(VARCHAR) TO ROLE PROVISIONER;'
        in sql
    )


GRANTS = [
    {"privilege": "USAGE", "granted_on": "DATABASE", "name": "D"},
    {"privilege": "USAGE", "granted_on": "WAREHOUSE", "name": "W"},
    {"privilege": "USAGE", "granted_on": "SCHEMA", "name": "D.MARTS"},
    {"privilege": "SELECT", "granted_on": "TABLE", "name": "D.MARTS.ORDERS"},
    {"privilege": "INSERT", "granted_on": "TABLE", "name": "D.MARTS.ORDERS"},
    {"privilege": "SELECT", "granted_on": "TABLE", "name": "OTHER.MARTS.ORDERS"},
    {"privilege": "SELECT", "granted_on": "VIEW", "name": "DX.MARTS.V"},
]
FUTURE = [
    {"privilege": "SELECT", "grant_on": "TABLE", "name": "D.MARTS.<TABLE>"},
    {"privilege": "SELECT", "grant_on": "VIEW", "name": "D.<VIEW>"},
    {"privilege": "SELECT", "grant_on": "TABLE", "name": "OTHER.MARTS.<TABLE>"},
]


def test_read_grants_returns_the_targets_read_grants_in_the_database_only():
    from vecten_shared.identity_setup import ACCESS_VERSION

    handler = _script().split("$$")[5]
    output = _run(handler, ["TARGET"], ["FINANCE"], owned=True, grants=GRANTS, future=FUTURE)
    assert output["error"] is None, output
    assert output["value"] == {
        "version": ACCESS_VERSION,
        "grants": [
            {"privilege": "USAGE", "granted_on": "SCHEMA", "name": "D.MARTS"},
            {"privilege": "SELECT", "granted_on": "TABLE", "name": "D.MARTS.ORDERS"},
        ],
        "future": [
            {"privilege": "SELECT", "grant_on": "TABLE", "name": "D.MARTS.<TABLE>"},
            {"privilege": "SELECT", "grant_on": "VIEW", "name": "D.<VIEW>"},
        ],
    }
    assert all(s.startswith("SHOW ") for s in output["statements"])


@pytest.mark.parametrize(
    ("target", "owned", "error"),
    [
        ("ANALYST", False, None),
        ("FINANCE", True, None),
        ("FINANCE", False, "not owned by provisioner"),
        ("READER", True, "not a bounded reader role"),
        ("PROVISIONER", True, "not a bounded reader role"),
        ("SECURITYADMIN", True, "not a bounded reader role"),
        ("SVC_ETL", True, "not a bounded reader role"),
        ('x";SHOW USERS;--', True, "not a bounded reader role"),
        (None, True, "not a bounded reader role"),
    ],
)
def test_read_grants_keeps_read_grants_target_bounds(target, owned, error):
    handler = _script().split("$$")[5]
    output = _run(handler, ["TARGET"], [target], owned=owned)
    if error is None:
        assert output["error"] is None, output
    else:
        assert error in output["error"], output
        assert not any(s.startswith("SHOW GRANTS TO") for s in output["statements"])


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("FINANCE_ACME_DEV", None),
        ("ANALYST_ACME_DEV", None),
        ("READER_ACME_DEV", "not a bounded reader role"),
        ("PROVISIONER_ACME_DEV", "not a bounded reader role"),
        ("LOADER_ACME_DEV", "not a bounded reader role"),
        ("FINANCE_ACME_PROD", "not a role of this environment"),
    ],
)
def test_read_grants_in_a_shared_account_bounds_targets_by_the_environments_names(target, error):
    sql = shared_script()
    handler = read_grant_body(sql[sql.index("READ_GRANTS(TARGET VARCHAR)") :])
    output = _run(handler, ["TARGET"], [target], owned=True, owner="PROVISIONER_ACME_DEV")
    if error is None:
        assert output["error"] is None, output
        assert output["value"]["grants"] == [] and output["value"]["future"] == []
    else:
        assert error in output["error"], output
        assert not any(s.startswith("SHOW GRANTS TO") for s in output["statements"])


KIND_RUN = r"""
const fs = require('fs');
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
const statements = [];
const held = p.held.slice(), future = p.future.slice();
const rows = (list) => {
    let i = -1;
    return {next: () => ++i < list.length, getColumnValue: (key) => list[i][key]};
};
const types = 'TABLE|VIEW|MATERIALIZED VIEW|EXTERNAL TABLE|DYNAMIC TABLE|ICEBERG TABLE';
const single = new RegExp('^(GRANT|REVOKE) SELECT ON (' + types + ') (\\S+) ');
const snowflake = {execute: ({sqlText}) => {
    if (sqlText.startsWith('SHOW GRANTS ON ROLE')) {
        return rows([{privilege:'OWNERSHIP', granted_to:'ROLE', grantee_name:'PROVISIONER'}]);
    }
    if (sqlText.startsWith('SELECT TABLE_TYPE')) return rows([{1: p.tableType}]);
    if (sqlText.startsWith('SHOW GRANTS TO ROLE')) return rows(held);
    if (sqlText.startsWith('SHOW FUTURE GRANTS TO ROLE')) return rows(future);
    const refused = p.refuse.find(part => sqlText.indexOf(part) !== -1);
    if (refused) throw new Error('SQL compilation error: ' + refused + ' is not supported');
    statements.push(sqlText);
    // A statement on the wrong object type of a relation succeeds and changes nothing.
    const m = sqlText.match(single);
    if (m && p.objectType === m[2]) {
        const name = m[3].replace(/"/g, '');
        if (m[1] === 'GRANT') held.push({privilege: 'SELECT', granted_on: m[2], name});
        else held.splice(held.findIndex(g => g.name === name), 1);
    }
    return rows([]);
}};
const arg = v => v === null ? undefined : (typeof v === 'boolean' ? Number(v) : v);
let value = null, error = null;
try {
    value = new Function('snowflake','TARGET','SCHEMA_NAME','RELATION_NAME','ENABLED',p.handler)(
        snowflake, 'FINANCE', 'MARTS', arg(p.relation), arg(p.enabled));
} catch (e) { error = String(e); }
process.stdout.write(JSON.stringify({value, error, statements}));
"""


def _kinds(
    relation=None,
    enabled=True,
    table_type="BASE TABLE",
    object_type=None,
    refuse=(),
    held=(),
    future=(),
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed to execute the Snowflake JavaScript handler locally")
    handler = _script().split("$$")[1]
    result = subprocess.run(
        [node, "-e", KIND_RUN],
        input=json.dumps(
            dict(
                handler=handler,
                relation=relation,
                enabled=enabled,
                tableType=table_type,
                objectType=object_type,
                refuse=list(refuse),
                held=list(held),
                future=list(future),
            )
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("enabled", [True, False])
def test_a_schema_grant_covers_every_relation_kind_now_and_in_future(enabled):
    output = _kinds(enabled=enabled)
    assert output["error"] is None and output["value"] == "ok", output
    verb, direction = ("GRANT", "TO") if enabled else ("REVOKE", "FROM")
    for kind in KINDS:
        for shape in ("ALL", "FUTURE"):
            statement = (
                f'{verb} SELECT ON {shape} {kind} IN SCHEMA "D"."MARTS" {direction} ROLE "FINANCE"'
            )
            assert statement in output["statements"]


def test_a_kind_the_account_refuses_on_a_grant_is_reported_and_the_others_still_apply():
    output = _kinds(refuse=["ICEBERG TABLES"])
    assert output["error"] is None, output
    assert output["value"].startswith("refused: ")
    refusals = json.loads(output["value"].removeprefix("refused: "))
    assert [r["kind"] for r in refusals] == ["ALL ICEBERG TABLES", "FUTURE ICEBERG TABLES"]
    assert all("ICEBERG TABLES is not supported" in r["error"] for r in refusals)
    assert any("DYNAMIC TABLES" in s for s in output["statements"])


DYNAMIC_FUTURE = {
    "privilege": "SELECT",
    "grant_on": "DYNAMIC_TABLE",
    "name": "D.MARTS.<DYNAMIC_TABLE>",
}


def test_a_revoke_a_kind_refuses_fails_while_the_role_still_holds_a_read_in_the_schema():
    output = _kinds(enabled=False, refuse=["DYNAMIC TABLES"], future=[DYNAMIC_FUTURE])
    assert output["error"] is not None, output
    assert "DYNAMIC TABLES is not supported" in output["error"]
    assert "still holds" in output["error"]
    assert 'REVOKE USAGE ON SCHEMA "D"."MARTS" FROM ROLE "FINANCE"' in output["statements"]


def test_a_revoke_a_kind_refuses_succeeds_once_nothing_is_held_in_the_schema():
    elsewhere = {"privilege": "SELECT", "granted_on": "TABLE", "name": "D.CORE.ORDERS"}
    output = _kinds(enabled=False, refuse=["ICEBERG TABLES"], held=[elsewhere])
    assert output["error"] is None and output["value"] == "ok", output


def test_a_refused_table_grant_fails_the_whole_call():
    output = _kinds(refuse=["ALL TABLES"])
    assert "ALL TABLES is not supported" in output["error"]


@pytest.mark.parametrize(
    ("table_type", "object_type", "refuse", "expected"),
    [
        ("BASE TABLE", "TABLE", (), ["TABLE"]),
        ("VIEW", "VIEW", (), ["VIEW"]),
        ("MATERIALIZED VIEW", "MATERIALIZED VIEW", (), ["MATERIALIZED VIEW"]),
        ("EXTERNAL TABLE", "EXTERNAL TABLE", (), ["EXTERNAL TABLE"]),
        ("BASE TABLE", "DYNAMIC TABLE", ("ON TABLE",), ["DYNAMIC TABLE"]),
        ("BASE TABLE", "DYNAMIC TABLE", (), ["TABLE", "DYNAMIC TABLE"]),
        ("BASE TABLE", "ICEBERG TABLE", (), ["TABLE", "DYNAMIC TABLE", "ICEBERG TABLE"]),
    ],
)
@pytest.mark.parametrize("enabled", [True, False])
def test_a_dataset_grant_names_its_relations_kind_and_reads_it_back(
    table_type, object_type, refuse, expected, enabled
):
    held = (
        []
        if enabled
        else [{"privilege": "SELECT", "granted_on": object_type, "name": "D.MARTS.ORDERS"}]
    )
    output = _kinds(
        relation="ORDERS",
        enabled=enabled,
        table_type=table_type,
        object_type=object_type,
        refuse=refuse,
        held=held,
    )
    assert output["error"] is None and output["value"] == "ok", output
    verb, direction = ("GRANT", "TO") if enabled else ("REVOKE", "FROM")
    selects = [s for s in output["statements"] if " SELECT ON " in s]
    assert selects == [
        f'{verb} SELECT ON {kind} "D"."MARTS"."ORDERS" {direction} ROLE "FINANCE"'
        for kind in expected
    ]


@pytest.mark.parametrize("enabled", [True, False])
def test_a_dataset_grant_no_object_type_changes_fails(enabled):
    held = (
        []
        if enabled
        else [{"privilege": "SELECT", "granted_on": "TABLE", "name": "D.MARTS.ORDERS"}]
    )
    output = _kinds(relation="ORDERS", enabled=enabled, object_type="OTHER", held=held)
    assert output["error"] is not None, output
    assert "ORDERS" in output["error"]


def test_a_base_table_no_object_type_accepts_fails_with_the_first_refusal():
    output = _kinds(relation="ORDERS", refuse=("ON TABLE", "ON DYNAMIC TABLE", "ON ICEBERG TABLE"))
    assert "ON TABLE is not supported" in output["error"]
