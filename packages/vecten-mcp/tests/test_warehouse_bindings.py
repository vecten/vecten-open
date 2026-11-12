"""Each adapter names a role that already exists, prints the binding statements, and reads
whether a group role holds its bound role."""

import psycopg
import pytest
from snowflake.connector.errors import ProgrammingError

from vecten_mcp.warehouse.base import RoleExists
from vecten_mcp.warehouse.postgres import PostgresAdapter
from vecten_mcp.warehouse.snowflake import SnowflakeAdapter


class Cursor:
    description = [("privilege",), ("granted_on",), ("name",)]

    def __init__(self, answer):
        self.answer, self.statements = answer, []

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self

    def fetchall(self):
        return self.answer

    def fetchone(self):
        return self.answer[0] if self.answer else None

    def close(self):
        pass


def snowflake(answer):
    cursor = Cursor(answer)
    adapter = SnowflakeAdapter(lambda: cursor, None, None, account="a", database="D", warehouse="W")
    return adapter, cursor


def postgres(answer):
    cursor = Cursor(answer)
    return PostgresAdapter(lambda: cursor, None), cursor


def test_snowflake_names_a_role_that_already_exists():
    adapter, _ = snowflake(ProgrammingError(msg="Object 'FINANCE' already exists.", errno=2002))
    with pytest.raises(RoleExists):
        adapter.create_group("finance")
    adapter, _ = snowflake(ProgrammingError(msg="Insufficient privileges", errno=3001))
    with pytest.raises(ProgrammingError):
        adapter.create_group("finance")


def test_postgres_names_a_role_that_already_exists():
    adapter, _ = postgres(psycopg.errors.DuplicateObject('role "finance" already exists'))
    with pytest.raises(RoleExists):
        adapter.create_group("finance")


def test_snowflake_binding_statements_and_the_role_read_back():
    adapter, cursor = snowflake(
        [
            ("USAGE", "ROLE", "FINANCE_ANALYST"),
            ("USAGE", "SCHEMA", "D.MARTS"),
        ]
    )
    assert adapter.bind_statement("finance_analyst", "finance") == (
        'GRANT ROLE "FINANCE_ANALYST" TO ROLE "FINANCE";'
    )
    assert adapter.bind_statement("finance_analyst", "finance", grant=False) == (
        'REVOKE ROLE "FINANCE_ANALYST" FROM ROLE "FINANCE";'
    )
    assert adapter.review_statement("finance_analyst") == 'SHOW GRANTS TO ROLE "FINANCE_ANALYST";'
    assert adapter.holds_role("finance", "finance_analyst")
    assert not adapter.holds_role("finance", "board_reader")
    assert cursor.statements[0] == ('SHOW GRANTS TO ROLE "FINANCE"', None)


def test_postgres_binding_statements_and_the_inherited_membership_read_back():
    adapter, cursor = postgres([(True,)])
    assert adapter.bind_statement("finance_analyst", "finance") == (
        'GRANT "finance_analyst" TO "finance" WITH INHERIT TRUE;'
    )
    assert adapter.bind_statement("finance_analyst", "finance", grant=False) == (
        'REVOKE "finance_analyst" FROM "finance";'
    )
    assert "finance_analyst" in adapter.review_statement("finance_analyst")
    assert adapter.holds_role("finance", "finance_analyst")
    [(statement, params)] = cursor.statements
    assert "pg_auth_members" in statement and "inherit_option" in statement
    assert params == ("finance", "finance_analyst")
