"""Each adapter reads the read grants managed roles hold, and the installed procedures' version."""

import json

import pytest

from vecten_mcp.warehouse.postgres import PostgresAdapter
from vecten_mcp.warehouse.snowflake import SnowflakeAdapter
from vecten_shared.identity_setup import ACCESS_VERSION


class SnowflakeConnection:
    """Answers READ_GRANT's version call and READ_GRANTS per target from `answers`."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.answer = self.answers(sql, params)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self

    def fetchone(self):
        return (self.answer,)

    def close(self):
        pass


def snowflake(answers):
    connection = SnowflakeConnection(answers)
    adapter = SnowflakeAdapter(
        lambda: connection, None, None, account="a", database="D", warehouse="W"
    )
    return adapter, connection


def test_snowflake_version_is_what_read_grant_reports_with_every_argument_null():
    adapter, connection = snowflake(lambda sql, params: f"vecten-access {ACCESS_VERSION}")
    assert adapter.access_version() == ACCESS_VERSION
    assert connection.calls == [('CALL "D"."VECTEN_ACCESS".READ_GRANT(NULL,NULL,NULL,NULL)', None)]


def test_a_snowflake_install_of_the_previous_release_is_older_than_this_server():
    # The release that added READ_GRANTS installed version 2, whose READ_GRANT refuses area
    # schemas and grants tables and views only.
    adapter, _ = snowflake(lambda sql, params: "vecten-access 2")
    assert adapter.access_version() == 2 < ACCESS_VERSION


@pytest.mark.parametrize(
    "message",
    [
        "target is not a bounded reader role",
        "Unknown user-defined function D.VECTEN_ACCESS.READ_GRANT.",
        "Procedure 'READ_GRANT' does not exist or not authorized.",
    ],
)
def test_snowflake_old_or_missing_procedure_is_version_one(message):
    from snowflake.connector.errors import ProgrammingError

    refused = ProgrammingError(msg=message, errno=100132)
    adapter, _ = snowflake(lambda sql, params: refused)
    assert adapter.access_version() == 1


def test_snowflake_any_other_refusal_of_the_version_call_is_a_failure():
    from snowflake.connector.errors import ProgrammingError

    refused = ProgrammingError(msg="Warehouse 'W' cannot be resumed", errno=606)
    adapter, _ = snowflake(lambda sql, params: refused)
    with pytest.raises(ProgrammingError):
        adapter.access_version()


def test_snowflake_read_grants_calls_read_grants_per_role_and_names_each_grant():
    held = {
        "version": ACCESS_VERSION,
        "grants": [
            {"privilege": "USAGE", "granted_on": "SCHEMA", "name": "D.MARTS"},
            {"privilege": "SELECT", "granted_on": "TABLE", "name": "D.MARTS.ORDERS"},
            {"privilege": "SELECT", "granted_on": "VIEW", "name": 'D.RAW_APP."Mixed.Case"'},
            {"privilege": "SELECT", "granted_on": "TABLE", "name": 'D.RAW_APP."ORDERS"'},
            {"privilege": "USAGE", "granted_on": "SCHEMA", "name": 'D."marts"'},
            {"privilege": "SELECT", "granted_on": "DYNAMIC_TABLE", "name": "D.MARTS.LIVE"},
            {"privilege": "USAGE", "granted_on": "DATABASE", "name": "D"},
            {"privilege": "SELECT", "granted_on": "TABLE", "name": "OTHER.MARTS.ORDERS"},
        ],
        "future": [
            {"privilege": "SELECT", "grant_on": "TABLE", "name": "D.MARTS.<TABLE>"},
            {"privilege": "SELECT", "grant_on": "VIEW", "name": "D.MARTS.<VIEW>"},
            {"privilege": "SELECT", "grant_on": "TABLE", "name": "D.<TABLE>"},
            {"privilege": "SELECT", "grant_on": "DYNAMIC_TABLE", "name": "D.MARTS.<DYNAMIC_TABLE>"},
        ],
    }

    def answers(sql, params):
        return json.dumps(held if params == ("ANALYST",) else {**held, "grants": [], "future": []})

    adapter, connection = snowflake(answers)
    assert adapter.read_grants({"analyst", "finance"}) == {
        ("analyst", "usage", "marts", None, "schema"),
        ("analyst", "usage", '"marts"', None, "schema"),
        ("analyst", "select", "marts", "orders", "table"),
        ("analyst", "select", "marts", "live", "dynamic_table"),
        ("analyst", "select", "raw_app", '"Mixed.Case"', "view"),
        ("analyst", "select", "raw_app", "orders", "table"),
        ("analyst", "future", "marts", None, "table"),
        ("analyst", "future", "marts", None, "view"),
        ("analyst", "future", "marts", None, "dynamic_table"),
        ("analyst", "future", None, None, "table"),
    }
    assert sorted(connection.calls) == [
        ('CALL "D"."VECTEN_ACCESS".READ_GRANTS(%s)', ("ANALYST",)),
        ('CALL "D"."VECTEN_ACCESS".READ_GRANTS(%s)', ("FINANCE",)),
    ]


def test_snowflake_read_grants_refuses_a_reserved_role_before_any_call():
    adapter, connection = snowflake(lambda sql, params: "{}")
    with pytest.raises(ValueError):
        adapter.read_grants({"admin_x"})
    assert connection.calls == []


@pytest.mark.parametrize(
    ("name", "grant_option", "valid"),
    [
        ("D.VECTEN_ACCESS.READ_GRANTS(VARCHAR)", "false", True),
        ("D.VECTEN_ACCESS.READ_GRANTS(VARCHAR)", "true", False),
        ("D.VECTEN_ACCESS.READ_GRANTS(VARCHAR, VARCHAR)", "false", False),
        ("OTHER.VECTEN_ACCESS.READ_GRANTS(VARCHAR)", "false", False),
    ],
)
def test_snowflake_scope_bounds_usage_on_read_grants(name, grant_option, valid):
    class Connection:
        description = [("privilege",), ("granted_on",), ("name",), ("grant_option",)]

        def cursor(self):
            return self

        def execute(self, query):
            self.query = query
            return self

        def fetchall(self):
            return [] if "FUTURE" in self.query else [("USAGE", "PROCEDURE", name, grant_option)]

        def close(self):
            pass

    adapter = SnowflakeAdapter(Connection, None, None, account="a", database="D", warehouse="W")
    if valid:
        adapter.check_scope({})
    else:
        with pytest.raises(ValueError, match="unexpected grant"):
            adapter.check_scope({})


def test_snowflake_scope_accepts_usage_on_read_grants():
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
            return [("USAGE", "PROCEDURE", "D.VECTEN_ACCESS.READ_GRANTS(VARCHAR)", "false")]

        def close(self):
            pass

    adapter = SnowflakeAdapter(Connection, None, None, account="a", database="D", warehouse="W")
    adapter.check_scope({})


class PostgresConnection:
    def __init__(self, rows):
        self.rows, self.statements = rows, []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        pass


def test_postgres_reads_the_catalogs_so_no_procedure_version_limits_it():
    assert PostgresAdapter(None, None).access_version() == ACCESS_VERSION


def test_postgres_read_grants_reads_the_catalog_acls_of_the_given_roles():
    rows = [
        ("analyst", "usage", "marts", None, "schema"),
        ("analyst", "select", "marts", "orders", "table"),
        ("finance", "future", "core", None, "table"),
        ("finance", "future", None, None, "table"),
    ]
    connection = PostgresConnection(rows)
    adapter = PostgresAdapter(lambda: connection, None)
    assert adapter.read_grants({"analyst", "finance"}) == set(rows)
    [(statement, params)] = connection.statements
    assert params == (["analyst", "finance"],)
    for catalog in ("pg_namespace", "nspacl", "relacl", "pg_default_acl", "defaclacl"):
        assert catalog in statement
    assert "relkind IN ('r','p','v','m','f')" in statement
    assert "LEFT JOIN pg_catalog.pg_namespace" in statement
