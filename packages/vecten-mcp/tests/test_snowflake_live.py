"""The MCP against a real Snowflake account: provisioning, External OAuth, rotation, golden SQL.

Runs only with VECTEN_SF_CI_ACCOUNT and VECTEN_SF_CI_PRIVATE_KEY set (the snowflake-live
workflow, started by hand before a release), as the service user VECTEN_CI with the role
VECTEN_CI_ADMIN. The provisioner script's role, user and integration names are account-wide,
so the fixture uses CI-specific names and verifies ownership before each cleanup.
"""

import io
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

ACCOUNT = os.environ.get("VECTEN_SF_CI_ACCOUNT")
PRIVATE_KEY = os.environ.get("VECTEN_SF_CI_PRIVATE_KEY")
pytestmark = [
    pytest.mark.snowflake_live,
    # The connector's certificate status check calls this pyOpenSSL API on every connect.
    pytest.mark.filterwarnings("ignore:X509.get_subject is deprecated:DeprecationWarning"),
    pytest.mark.skipif(
        not (ACCOUNT and PRIVATE_KEY), reason="needs VECTEN_SF_CI_ACCOUNT and _PRIVATE_KEY"
    ),
]

ADMIN_USER, ADMIN_ROLE, WAREHOUSE = "VECTEN_CI", "VECTEN_CI_ADMIN", "VECTEN_CI_WH"
DATABASE = "VECTEN_CI"
# Snowflake never fetches this issuer: the integration holds the signing keys inline.
ISSUER = "https://mcp.ci.vecten.invalid/snowflake-live"
PERSON = "vecten_ci_person"
TPCDS = Path(__file__).parents[2] / "vecten-shared" / "tests" / "fixtures" / "tpcds"
SAMPLE = "SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL"
# One day of two stores: the slice the fixture model's descriptions name.
FIXTURE_SQL = f"""
CREATE DATABASE {DATABASE};
CREATE SCHEMA {DATABASE}.CORE;
CREATE SCHEMA {DATABASE}.MARTS;
CREATE TABLE {DATABASE}.CORE.DATE_DIM AS SELECT * FROM {SAMPLE}.DATE_DIM WHERE D_YEAR = 2002;
CREATE TABLE {DATABASE}.CORE.STORE AS SELECT * FROM {SAMPLE}.STORE;
CREATE TABLE {DATABASE}.MARTS.STORE_SALES AS SELECT ss.* FROM {SAMPLE}.STORE_SALES ss
  JOIN {DATABASE}.CORE.DATE_DIM d ON ss.SS_SOLD_DATE_SK = d.D_DATE_SK
  WHERE d.D_DATE = '2002-01-01' AND ss.SS_STORE_SK IN (1, 2);
CREATE TABLE {DATABASE}.CORE.ITEM AS SELECT * FROM {SAMPLE}.ITEM
  WHERE I_ITEM_SK IN (SELECT SS_ITEM_SK FROM {DATABASE}.MARTS.STORE_SALES);
CREATE TABLE {DATABASE}.CORE.CUSTOMER AS SELECT * FROM {SAMPLE}.CUSTOMER
  WHERE C_CUSTOMER_SK IN (SELECT SS_CUSTOMER_SK FROM {DATABASE}.MARTS.STORE_SALES);
"""


def _connect(**kwargs):
    import snowflake.connector

    return snowflake.connector.connect(account=ACCOUNT, login_timeout=30, **kwargs)


def _admin():
    from vecten_shared.keypair import load_private_key

    return _connect(
        user=ADMIN_USER,
        role=ADMIN_ROLE,
        warehouse=WAREHOUSE,
        private_key=load_private_key(PRIVATE_KEY, "VECTEN_SF_CI_PRIVATE_KEY"),
    )


def _rows(cursor, sql):
    cursor.execute(sql)
    columns = [c[0].lower() for c in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _statements(sql):
    from snowflake.connector.util_text import split_statements

    return [
        s.strip() for s, _ in split_statements(io.StringIO(sql), remove_comments=True) if s.strip()
    ]


def _owner(cursor, kind, name):
    rows = _rows(cursor, f"SHOW {kind} LIKE '{name}'")
    # Underscores are wildcards in LIKE, even in unquoted Snowflake object names.
    for row in rows:
        if row["name"] != name:
            continue
        if kind == "INTEGRATIONS":
            grants = _rows(cursor, f"SHOW GRANTS ON INTEGRATION {name}")
            for grant in grants:
                if grant["privilege"] == "OWNERSHIP" and grant["granted_to"] == "ROLE":
                    return grant["grantee_name"]
        elif row.get("owner"):
            return row["owner"]
        pytest.fail(f"could not establish ownership of {name}")
    return None


def _teardown(cursor, names):
    # Check the entire set before the first write, including after a partial setup.
    objects = (
        ("USERS", "USER", PERSON.upper(), names.provisioner_role),
        ("INTEGRATIONS", "SECURITY INTEGRATION", names.integration, ADMIN_ROLE),
        ("USERS", "USER", names.provisioner_user, ADMIN_ROLE),
        ("ROLES", "ROLE", names.analyst_role, ADMIN_ROLE),
        ("DATABASES", "DATABASE", names.database, ADMIN_ROLE),
        ("ROLES", "ROLE", names.provisioner_role, ADMIN_ROLE),
    )
    drops = []
    for listing, kind, name, expected in objects:
        owner = _owner(cursor, listing, name)
        if owner is None:
            continue
        if owner != expected:
            pytest.fail(f"{name} belongs to {owner}, expected {expected}; refusing fixture cleanup")
        drops.append((kind, name))

    if ("USER", PERSON.upper()) in drops:
        if ("ROLE", names.provisioner_role) not in drops:
            pytest.fail(f"could not verify the role owning {PERSON}")
        cursor.execute(f"GRANT ROLE {names.provisioner_role} TO ROLE {ADMIN_ROLE}")
    # The provisioner role must still exist while its fixture user is being dropped.
    for kind, name in drops:
        cursor.execute(f"DROP {kind} IF EXISTS {name}")


def _public_body(pem):
    from vecten_shared.keypair import load_private_key, public_body

    return public_body(load_private_key(pem, "signing key"))


def _names():
    """The fixed layout's names, over the CI account's database and warehouse."""
    from dataclasses import replace

    from vecten_shared.config import VectenConfig
    from vecten_shared.provider import snowflake_names

    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "vecten-ci", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake"},
        }
    )
    return replace(
        snowflake_names(config, "ci"),
        account=ACCOUNT,
        database=DATABASE,
        warehouse=WAREHOUSE,
        provisioner_role="VECTEN_CI_PROVISIONER",
        provisioner_user="VECTEN_CI_PROVISIONER_USER",
        analyst_role="VECTEN_CI_ANALYST",
        integration="VECTEN_CI_EXTERNAL_OAUTH",
    )


class Secrets:
    def __init__(self):
        self.values = {}

    def list_secrets(self):
        return list(self.values)

    def get_secret(self, name):
        return self.values[name]

    def put_secret(self, name, value):
        self.values[name] = value


@pytest.fixture(scope="module")
def account():
    """The account with the CI database, the real provisioner script and its integration."""
    from vecten_mcp.auth.keys import SigningKeys
    from vecten_mcp.warehouse.snowflake import SnowflakeAdapter
    from vecten_shared.identity_setup import provisioner_script
    from vecten_shared.keypair import generate_one_line, load_private_key, public_body

    names = _names()
    with ExitStack() as cleanup:
        admin = _admin()
        cleanup.callback(admin.close)
        cursor = admin.cursor()
        cleanup.callback(cursor.close)
        _teardown(cursor, names)
        cleanup.callback(_teardown, cursor, names)
        for statement in _statements(FIXTURE_SQL):
            cursor.execute(statement)
        provisioner_key = load_private_key(generate_one_line(), "provisioner")
        keys = SigningKeys(Secrets(), ISSUER)
        audience = names.audience
        script = provisioner_script(
            "snowflake", issuer=ISSUER, public_key=public_body(provisioner_key), names=names
        )
        for statement in _statements(script):
            if statement.upper().startswith("ALTER SECURITY INTEGRATION"):
                continue
            if statement.upper().startswith("CREATE SECURITY INTEGRATION"):
                # The product statement, with the signing key inline in place of the JWKS URL.
                inline = f"EXTERNAL_OAUTH_RSA_PUBLIC_KEY = '{_public_body(keys.ring['private'])}'"
                url = f"EXTERNAL_OAUTH_JWS_KEYS_URL = '{ISSUER}/jwks'"
                assert url in statement
                statement = statement.replace(url, inline)
            cursor.execute(statement)
        adapter = SnowflakeAdapter(
            lambda: _connect(
                user=names.provisioner_user,
                role=names.provisioner_role,
                private_key=provisioner_key,
                database=DATABASE,
                warehouse=WAREHOUSE,
            ),
            lambda **kwargs: _connect(**{k: v for k, v in kwargs.items() if k != "account"}),
            lambda **kwargs: keys.snowflake_token(**kwargs, audience=audience),
            names=names,
        )
        # The script grants ANALYST no data: the MCP's first reconciliation applies the default
        # group's schema grants through READ_GRANT, as here.
        for schema in ("core", "marts"):
            adapter.schema_grant("analyst", schema)
        yield type("Account", (), {"cursor": cursor, "adapter": adapter, "keys": keys})


def test_the_provisioner_holds_exactly_its_scope(account):
    account.adapter.check_scope({})


def test_a_person_is_created_without_a_credential(account):
    account.adapter.create_account(PERSON, "human")
    account.adapter.membership("analyst", PERSON)
    [user] = [u for u in _rows(account.cursor, "SHOW USERS") if u["name"] == PERSON.upper()]
    assert user["type"] == "PERSON"
    assert str(user["has_password"]).lower() == "false"
    assert str(user["has_rsa_public_key"]).lower() == "false"
    account.adapter.check_scope({PERSON: {"kind": "human"}})


def _session(account):
    return account.adapter.connect_principal(
        {"warehouse_name": PERSON}, {"analyst"}, time.time() + 600
    )


def test_an_external_oauth_session_runs_as_the_person(account):
    connection = _session(account)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_DATABASE()")
        assert cursor.fetchone() == (PERSON.upper(), account.adapter.names.analyst_role, DATABASE)
    finally:
        connection.close()


def _compiled(case):
    from vecten_shared.osi.semantic import SemanticModel

    cases = json.loads((TPCDS / "cases.json").read_text())["compiled"]
    model = SemanticModel.load(TPCDS / "osi", kind="snowflake", commit="fixture")
    result = model.compile(cases[case], row_cap=1000)
    assert result["resolution"] == "compiled", result
    return result["sql"]


EXPECTED = {
    "net_paid_and_lines_by_store": (
        "SELECT SS_STORE_SK, SUM(SS_NET_PAID), COUNT(SS_ITEM_SK) FROM MARTS.STORE_SALES "
        "GROUP BY 1 ORDER BY 1"
    ),
    "sales_and_profit_by_day": (
        "SELECT SS_SOLD_DATE_SK, SUM(SS_EXT_SALES_PRICE), SUM(SS_NET_PROFIT) "
        "FROM MARTS.STORE_SALES GROUP BY 1 ORDER BY 1"
    ),
    "all_metrics": (
        "SELECT SUM(SS_EXT_SALES_PRICE), SUM(SS_NET_PROFIT), SUM(SS_NET_PAID), "
        "COUNT(SS_ITEM_SK) FROM MARTS.STORE_SALES"
    ),
}


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_golden_sql_answers_as_the_warehouse_does(account, case):
    account.cursor.execute(f"USE DATABASE {DATABASE}")
    account.cursor.execute(EXPECTED[case])
    expected = account.cursor.fetchall()
    assert expected and expected[0][0] is not None
    connection = _session(account)
    try:
        cursor = connection.cursor()
        cursor.execute(_compiled(case))
        assert cursor.fetchall() == expected
    finally:
        connection.close()


def test_a_rotation_keeps_person_sessions_working(account):
    from vecten_mcp.auth.keys import PUBLISH_AHEAD, ROTATE_AFTER

    keys = account.keys
    current = keys.ring["kid"]
    # The ring decides on its own timestamps; tokens keep real times, which Snowflake checks.
    keys.ring["created_at"] -= ROTATE_AFTER - PUBLISH_AHEAD
    keys.maintain()
    upcoming = keys.ring["next"]
    assert keys.ring["kid"] == current
    account.cursor.execute(
        f"ALTER SECURITY INTEGRATION {account.adapter.names.integration} SET "
        f"EXTERNAL_OAUTH_RSA_PUBLIC_KEY_2 = '{_public_body(upcoming['private'])}'"
    )
    upcoming["published_at"] -= PUBLISH_AHEAD
    keys.maintain()
    assert keys.ring["kid"] == upcoming["kid"] != current
    connection = _session(account)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT CURRENT_USER()")
        assert cursor.fetchone() == (PERSON.upper(),)
    finally:
        connection.close()


def test_shared_binding_verifies_nested_grants_as_the_provisioner(account):
    """The dedicated CI database serves as shared environment VECTEN_CI for this check."""
    from dataclasses import replace
    from uuid import uuid4

    from vecten_mcp.warehouse.snowflake import SnowflakeAdapter
    from vecten_shared.identity_setup import snowflake_access_helper

    token = uuid4().hex.upper()
    candidate, nested = f"CI_READER_{token}", f"CI_CHILD_{token}"
    sibling = f"VECTEN_TEST{token}"
    names = replace(account.adapter.names, suffix=f"_{DATABASE}")
    adapter = SnowflakeAdapter(account.adapter.provisioner_connect, None, None, names=names)
    execute = account.cursor.execute
    with ExitStack() as cleanup:
        for statement in _statements(snowflake_access_helper(names)):
            if ".CHECK_BINDING(" in statement:
                execute(statement)
                if statement.startswith("CREATE"):
                    cleanup.callback(
                        execute, f"DROP PROCEDURE {DATABASE}.VECTEN_ACCESS.CHECK_BINDING(VARCHAR)"
                    )
        for role in (candidate, nested):
            execute(f"CREATE ROLE {role}")
            cleanup.callback(execute, f"DROP ROLE {role}")
        execute(f"CREATE TRANSIENT DATABASE {sibling}")
        cleanup.callback(execute, f"DROP DATABASE {sibling}")
        execute(f"GRANT ROLE {nested} TO ROLE {candidate}")
        assert adapter.bound_role_problem(candidate, set()) is None
        execute(f"GRANT USAGE ON DATABASE {sibling} TO ROLE {nested}")
        assert sibling in adapter.bound_role_problem(candidate, set())
        execute(f"REVOKE USAGE ON DATABASE {sibling} FROM ROLE {nested}")
        assert adapter.bound_role_problem(candidate, set()) is None
