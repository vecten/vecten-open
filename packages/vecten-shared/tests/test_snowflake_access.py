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


def _script():
    return provisioner_script(
        "snowflake",
        database="D",
        warehouse="W",
        issuer="https://mcp.example",
        account="org-account",
    )


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
                                grantee_name:'PROVISIONER'}] : []);
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
