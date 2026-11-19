"""The MCP against a real Snowflake account: provisioning, External OAuth, rotation, golden SQL.

Runs only with VECTEN_SF_CI_ACCOUNT and VECTEN_SF_CI_PRIVATE_KEY set (the snowflake-live
workflow, started by hand before a release), as the service user VECTEN_CI with the role
VECTEN_CI_ADMIN. The provisioner script's role, user and integration names are account-wide,
so a run refuses an account holding an install it did not make, and drops everything it made.
"""

import io
import json
import os
import time
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
ISSUER = "https://mcp.ci.vecten.invalid"
PERSON = "vecten_ci_person"
TPCDS = Path(__file__).parents[2] / "vecten-shared" / "tests" / "fixtures" / "tpcds"
SAMPLE = "SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL"
# One day of two stores: the slice the fixture model's descriptions name.
FIXTURE_SQL = f"""
CREATE OR REPLACE DATABASE {DATABASE};
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
    found = _rows(cursor, f"SHOW {kind} LIKE '{name}'")
    return found[0]["owner"] if found else None


def _teardown(cursor):
    """Drops what a run makes, the users PROVISIONER created first, by inheriting its ownership."""
    if _owner(cursor, "ROLES", "PROVISIONER") == ADMIN_ROLE:
        cursor.execute(f"GRANT ROLE PROVISIONER TO ROLE {ADMIN_ROLE}")
        owned = _rows(cursor, "SHOW GRANTS TO ROLE PROVISIONER")
        for grant in owned:
            if grant["privilege"] == "OWNERSHIP" and grant["granted_on"] in ("USER", "ROLE"):
                kind = grant["granted_on"]
                if grant["name"] != "ANALYST":
                    cursor.execute(f'DROP {kind} IF EXISTS "{grant["name"]}"')
    for statement in (
        "DROP SECURITY INTEGRATION IF EXISTS VECTEN_EXTERNAL_OAUTH",
        "DROP USER IF EXISTS VECTEN_PROVISIONER",
        "DROP ROLE IF EXISTS PROVISIONER",
        "DROP ROLE IF EXISTS ANALYST",
        f"DROP DATABASE IF EXISTS {DATABASE}",
    ):
        cursor.execute(statement)


def _refuse_a_foreign_install(cursor):
    for kind, name in (
        ("ROLES", "PROVISIONER"),
        ("ROLES", "ANALYST"),
        ("USERS", "VECTEN_PROVISIONER"),
        ("INTEGRATIONS", "VECTEN_EXTERNAL_OAUTH"),
    ):
        owner = _owner(cursor, kind, name)
        if owner not in (None, ADMIN_ROLE):
            pytest.fail(f"{name} belongs to {owner}: another vecten install uses this account")


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
        snowflake_names(config, "ci"), account=ACCOUNT, database=DATABASE, warehouse=WAREHOUSE
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

    admin = _admin()
    cursor = admin.cursor()
    try:
        _refuse_a_foreign_install(cursor)
        _teardown(cursor)
        for statement in _statements(FIXTURE_SQL):
            cursor.execute(statement)
        provisioner_key = load_private_key(generate_one_line(), "provisioner")
        keys = SigningKeys(Secrets(), ISSUER)
        names = _names()
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
    finally:
        try:
            _teardown(cursor)
        finally:
            cursor.close()
            admin.close()


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
        {"warehouse_name": PERSON}, {"ANALYST"}, time.time() + 600
    )


def test_an_external_oauth_session_runs_as_the_person(account):
    connection = _session(account)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_DATABASE()")
        assert cursor.fetchone() == (PERSON.upper(), "ANALYST", DATABASE)
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
        "ALTER SECURITY INTEGRATION VECTEN_EXTERNAL_OAUTH SET "
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
