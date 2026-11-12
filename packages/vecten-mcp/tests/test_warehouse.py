from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from vecten_mcp.failures import NoWarehouseAccess
from vecten_mcp.warehouse.pool import PrincipalPools
from vecten_mcp.warehouse.postgres import PostgresAdapter
from vecten_mcp.warehouse.snowflake import SnowflakeAdapter
from vecten_shared.config import VectenConfig
from vecten_shared.provider import snowflake_names


def names(account="a", database="D", warehouse="W"):
    """The fixed layout's names, over a test account's database and warehouse."""
    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "acme", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "account": account},
        }
    )
    return replace(
        snowflake_names(config, "prod"), account=account, database=database, warehouse=warehouse
    )


class Connection:
    def __init__(self, owner):
        self.owner, self.closed = owner, False

    def close(self):
        self.closed = True


def test_postgres_source_check_uses_catalogs_without_requiring_table_read_grants():
    from unittest.mock import MagicMock

    from sqlglot import parse_one

    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (True,)
    adapter = PostgresAdapter(lambda: conn, lambda: None)
    assert adapter.source_exists(
        parse_one("SELECT * FROM warehouse.raw_app.orders").find(__import__("sqlglot").exp.Table)
    )
    statement, parameters = conn.execute.call_args.args
    assert "pg_catalog.pg_class" in statement
    assert parameters == ("warehouse", "raw_app", "orders")
    conn.close.assert_called_once()


def test_snowflake_source_check_calls_bounded_metadata_helper():
    from unittest.mock import MagicMock

    from sqlglot import exp, parse_one

    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = (True,)
    adapter = SnowflakeAdapter(lambda: conn, None, None, names=names())
    assert adapter.source_exists(parse_one('SELECT * FROM raw_app."Orders"').find(exp.Table))
    assert cursor.execute.call_args.args == (
        'CALL "D"."VECTEN_ACCESS".SOURCE_EXISTS(%s,%s)',
        ("RAW_APP", "Orders"),
    )
    assert not adapter.source_exists(
        parse_one("SELECT * FROM other.raw_app.orders").find(exp.Table)
    )


class Adapter:
    def __init__(self):
        self.opened, self.granted, self.revoked = [], [], []

    def open_membership(self, name):
        self.granted.append(name)

    def close_membership(self, name):
        self.revoked.append(name)

    def connect_principal(self, principal, roles, expires_at):
        c = Connection(principal["id"])
        self.opened.append(c)
        return c


@pytest.fixture
def principal():
    return dict(id="p", warehouse_name="jane", enabled=True, provisioned=True)


def test_two_sessions_do_not_revoke_each_others_membership(principal):
    adapter = Adapter()
    pools = PrincipalPools(adapter, clock=lambda: 10)
    with pools.acquire(principal, "s1", 100) as one:
        with pools.acquire(principal, "s2", 100) as two:
            assert one is not two
    pools.revoke_session("s1")
    assert not adapter.revoked
    pools.revoke_session("s2")
    assert adapter.granted == ["jane"] and adapter.revoked == ["jane"]
    assert all(c.closed for c in adapter.opened)


def test_concurrent_principals_never_share_connection(principal):
    pools = PrincipalPools(Adapter(), clock=lambda: 10)

    def run(i):
        p = dict(principal, id=str(i), warehouse_name=f"p{i}")
        with pools.acquire(p, f"s{i}", 100) as conn:
            return conn.owner

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(run, range(4))) == ["0", "1", "2", "3"]
    pools.close()


def test_idle_expiry_disable_and_unprovisioned_refusal(principal):
    now = [10]
    adapter = Adapter()
    pools = PrincipalPools(adapter, clock=lambda: now[0], idle_timeout=5)
    with pools.acquire(principal, "s", 100):
        pass
    now[0] = 16
    pools.expire()
    assert adapter.revoked == ["jane"]
    with pools.acquire(principal, "new", 100):
        pools.disable_principal("p")
    assert adapter.opened[-1].closed
    for p in (dict(principal, enabled=False), dict(principal, provisioned=False)):
        with pytest.raises(NoWarehouseAccess):
            with pools.acquire(p, "bad", 100):
                pass
    with pytest.raises(ValueError, match="expired"):
        with pools.acquire(principal, "old", 15):
            pass


def test_pool_bound_refuses_excess_concurrent_connections(principal):
    pools = PrincipalPools(Adapter(), max_connections=1, clock=lambda: 1)
    with pools.acquire(principal, "s", 100):
        with pytest.raises(ValueError, match="busy"):
            with pools.acquire(principal, "s", 100):
                pass


@pytest.mark.parametrize("operation", ["disable", "revoke", "expire"])
def test_failed_membership_revocation_is_retried_by_maintenance(principal, operation):
    class UnavailableOnce(Adapter):
        def close_membership(self, name):
            if not self.revoked:
                self.revoked.append("failed")
                raise ConnectionError("warehouse unavailable")
            super().close_membership(name)

    now = [1]
    adapter = UnavailableOnce()
    pools = PrincipalPools(adapter, clock=lambda: now[0])
    with pools.acquire(principal, "s", 10) as connection:
        pass
    with pytest.raises(ConnectionError):
        if operation == "disable":
            pools.disable_principal(principal["id"])
        elif operation == "revoke":
            pools.revoke_session("s")
        else:
            now[0] = 11
            pools.expire()
    assert connection.closed
    pools.expire()
    assert adapter.revoked == ["failed", "jane"]
    assert not pools.pools


class Cursor:
    description = None

    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class SQLConnection:
    def __init__(self, rows=()):
        self.statements = []
        self.rows = rows
        self.closed = False

    def execute(self, sql, params=None):
        from psycopg.sql import Composable

        self.statements.append((sql.as_string() if isinstance(sql, Composable) else sql, params))
        return Cursor(self.rows)

    def close(self):
        self.closed = True


def test_postgres_creation_and_session_sql_never_sets_person_password(principal):
    control, query = SQLConnection(), SQLConnection()
    adapter = PostgresAdapter(lambda: control, lambda: query)
    adapter.create_account("jane", "human")
    adapter.open_membership("jane")
    adapter.connect_principal(principal, {"analyst"}, 100)
    adapter.close_membership("jane")
    text = "\n".join(s for s, _ in control.statements)
    assert 'CREATE ROLE "jane" NOLOGIN' in text
    assert "PASSWORD" not in text
    assert "WITH SET TRUE, INHERIT FALSE" in text
    assert 'REVOKE "jane" FROM "impersonator"' in text
    assert query.statements[0][0] == 'SET ROLE "jane"'
    assert query.statements[1][1] == ("jane",)


@pytest.mark.parametrize("name", ["admin_jane", "loader", 'x";DROP ROLE postgres;--'])
def test_adapter_refuses_reserved_or_malformed_account(name):
    adapter = PostgresAdapter(lambda: SQLConnection(), lambda: SQLConnection())
    with pytest.raises(ValueError):
        adapter.create_account(name, "human")


def test_snowflake_person_service_and_oauth_connection(principal):
    statements, calls = [], []

    class SFConnection:
        def cursor(self):
            return self

        def execute(self, sql, params=None):
            statements.append((sql, params))
            return self

        def fetchall(self):
            return []

        def close(self):
            pass

    def connect(**kw):
        calls.append(kw)
        return SFConnection()

    minted = []
    adapter = SnowflakeAdapter(
        lambda: SFConnection(),
        connect,
        lambda **kw: minted.append(kw) or "temporary",
        names=names("org-account", "WAREHOUSE", "COMPUTE"),
    )
    adapter.create_account("jane", "human")
    adapter.create_account("svc_reporting", "service")
    adapter.connect_principal(principal, {"analyst"}, 100)
    assert "TYPE = PERSON" in statements[0][0]
    assert "TYPE = SERVICE" in statements[1][0]
    assert not any("PASSWORD" in sql or "RSA_PUBLIC_KEY" in sql for sql, _ in statements)
    assert calls[0]["authenticator"] == "oauth" and calls[0]["user"] == "JANE"
    assert minted[0] == {"subject": "JANE", "roles": {"ANALYST"}, "expires_at": 100}
    assert calls[0]["role"] == "ANALYST"


def test_delayed_acquire_rechecks_revoked_session_under_pool_lock(principal):
    from threading import Event

    sessions = {"s"}
    paused, resume = Event(), Event()

    def validate(snapshot, sid, roles):
        if sid not in sessions:
            raise ValueError("session revoked")
        return snapshot, roles

    pools = PrincipalPools(Adapter(), clock=lambda: 1, validate=validate)

    def delayed():
        paused.set()
        resume.wait(5)
        with pools.acquire(principal, "s", 100):
            return "opened"

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(delayed)
        assert paused.wait(5)
        sessions.remove("s")
        pools.revoke_session("s")
        resume.set()
        with pytest.raises(ValueError, match="revoked"):
            result.result()
    assert not pools.adapter.granted


def test_snowflake_owned_user_allowed_only_for_principal_registry_entry():
    class Connection:
        description = [("privilege",), ("granted_on",), ("name",), ("grant_option",)]

        def cursor(self):
            return self

        def execute(self, query):
            self.query = query
            return self

        def fetchall(self):
            if "FUTURE" in self.query:
                return []
            return [("OWNERSHIP", "USER", "JANE", "true")]

        def close(self):
            pass

    adapter = SnowflakeAdapter(Connection, None, None, names=names())
    adapter.check_scope({"jane": {"kind": "human"}})
    with pytest.raises(ValueError):
        adapter.check_scope({"jane": {"kind": "group"}})


def test_snowflake_role_ownership_requires_group_registry_kind():
    class Connection:
        description = [("privilege",), ("granted_on",), ("name",), ("grant_option",)]

        def cursor(self):
            return self

        def execute(self, query):
            self.query = query
            return self

        def fetchall(self):
            if "FUTURE" in self.query:
                return []
            return [("OWNERSHIP", "ROLE", "JANE", "true")]

        def close(self):
            pass

    adapter = SnowflakeAdapter(Connection, None, None, names=names())
    with pytest.raises(ValueError):
        adapter.check_scope({"jane": {"kind": "human"}})
    adapter.check_scope({"jane": {"kind": "group"}})


def test_postgres_schema_grants_fail_closed_until_owner_authority_is_configured():
    from contextlib import nullcontext

    connection = SQLConnection(rows=[(None,)])
    connection.transaction = nullcontext
    adapter = PostgresAdapter(lambda: connection, lambda: connection)
    with pytest.raises(ValueError, match="install schema access helpers"):
        adapter.schema_grant("analyst", "core")
    assert all(statement.startswith("SELECT") for statement, _ in connection.statements)


@pytest.mark.parametrize("grant", [True, False])
def test_snowflake_grants_call_bounded_owner_procedure(grant):
    statements = []

    class Connection:
        def cursor(self):
            return self

        def execute(self, sql, params=None):
            statements.append((sql, params))
            return self

        def close(self):
            pass

    adapter = SnowflakeAdapter(Connection, None, None, names=names())
    adapter.schema_grant("finance", "staging", grant)
    adapter.dataset_grant("finance", "D.CORE.orders", grant)
    assert statements == [
        ('CALL "D"."VECTEN_ACCESS".READ_GRANT(%s,%s,%s,%s)', ("FINANCE", "STAGING", None, grant)),
        ('CALL "D"."VECTEN_ACCESS".READ_GRANT(%s,%s,%s,%s)', ("FINANCE", "CORE", "ORDERS", grant)),
    ]
    with pytest.raises(ValueError):
        adapter.dataset_grant("finance", "other.CORE.orders")
    assert len(statements) == 2


@pytest.mark.parametrize("reject", [False, True])
def test_snowflake_secondary_roles_are_explicit_and_fail_closed(principal, reject):
    class Connection:
        closed = False
        statements = []

        def cursor(self):
            return self

        def execute(self, statement):
            self.statements.append(statement)
            if reject:
                raise RuntimeError("secondary roles rejected")

        def close(self):
            self.closed = True

    connection = Connection()
    adapter = SnowflakeAdapter(
        None,
        lambda **kwargs: connection,
        lambda **kwargs: "token",
        names=names(),
    )
    if reject:
        with pytest.raises(RuntimeError, match="secondary"):
            adapter.connect_principal(principal, {"analyst", "finance"}, 100)
        assert connection.closed
    else:
        assert adapter.connect_principal(principal, {"analyst", "finance"}, 100) is connection
    assert connection.statements == ['USE SECONDARY ROLES "ANALYST", "FINANCE"']


def test_snowflake_single_role_session_runs_no_secondary_roles_statement(principal):
    statements = []

    class Connection:
        def cursor(self):
            return self

        def execute(self, statement):
            statements.append(statement)

        def close(self):
            pass

    logins = []
    adapter = SnowflakeAdapter(
        None,
        lambda **kwargs: logins.append(kwargs) or Connection(),
        lambda **kwargs: "token",
        names=names(),
    )
    adapter.connect_principal(principal, {"analyst"}, 100)
    assert logins[0]["role"] == "ANALYST" and statements == []


@pytest.mark.parametrize(
    "schema,privilege,grantable,valid",
    [
        ("CORE", "SELECT", "true", True),
        ("MARTS", "SELECT", "true", True),
        ("PRIVATE", "SELECT", "true", False),
        ("CORE", "INSERT", "true", False),
        ("CORE", "SELECT", "false", False),
    ],
)
def test_snowflake_future_grants_are_checked(schema, privilege, grantable, valid):
    class Connection:
        description = [("privilege",), ("grant_on",), ("name",), ("grant_option",)]
        query = ""

        def cursor(self):
            return self

        def execute(self, query):
            self.query = query
            return self

        def fetchall(self):
            return (
                [(privilege, "TABLE", f"D.{schema}.<TABLE>", grantable)]
                if "FUTURE" in self.query
                else []
            )

        def close(self):
            pass

    adapter = SnowflakeAdapter(Connection, None, None, names=names())
    if valid:
        adapter.check_scope({})
    else:
        with pytest.raises(ValueError, match="future"):
            adapter.check_scope({})


def test_the_session_check_tells_a_principal_without_access_from_a_revoked_session():
    from vecten_mcp.identity import Identity
    from vecten_mcp.runtime_setup import session_validator
    from vecten_mcp.state import StateStore

    state = StateStore("sqlite://")
    person = state.create_principal(
        Identity("password", "jane@example.com", "jane@example.com", "Jane"), "jane"
    )
    state.put("sessions", "s1", {"principal_id": person["id"]}, expires_at=100)
    validate = session_validator(state, clock=lambda: 10)
    with pytest.raises(NoWarehouseAccess):
        validate(person, "s1", set())
    with pytest.raises(ValueError, match="revoked") as revoked:
        validate(person, "gone", set())
    assert not isinstance(revoked.value, NoWarehouseAccess)
