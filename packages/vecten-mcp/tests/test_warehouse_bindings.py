"""Each adapter names a role that already exists, prints the binding statements, and reads
whether a group role holds its bound role."""

import importlib.util
import sys
from pathlib import Path

import psycopg
import pytest
from snowflake.connector.errors import ProgrammingError

from vecten_mcp.warehouse.base import RoleExists
from vecten_mcp.warehouse.postgres import PostgresAdapter

_spec = importlib.util.spec_from_file_location(
    "bindings_warehouse_fakes", Path(__file__).with_name("warehouse_fakes.py")
)
fakes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fakes
_spec.loader.exec_module(fakes)
Cursor, names, snowflake, postgres = fakes.Cursor, fakes.names, fakes.snowflake, fakes.postgres


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
    review = adapter.review_statement("finance_analyst")
    assert "WITH RECURSIVE" in review and "'FINANCE_ANALYST'" in review
    assert "GRANTS_TO_ROLES" in review and "ACCOUNT" in review
    assert adapter.holds_role("finance", "finance_analyst")
    assert not adapter.holds_role("finance", "board_reader")
    assert cursor.statements[0] == ('SHOW GRANTS TO ROLE "FINANCE"', None)


def test_postgres_binding_statements_and_the_inherited_membership_read_back():
    adapter, cursor = postgres([(True,)])
    assert adapter.bind_statement("finance_analyst", "finance") == (
        'GRANT "finance_analyst" TO "finance" WITH INHERIT TRUE, SET FALSE;'
    )
    assert adapter.bind_statement("finance_analyst", "finance", grant=False) == (
        'REVOKE "finance_analyst" FROM "finance";'
    )
    assert "finance_analyst" in adapter.review_statement("finance_analyst")
    assert adapter.holds_role("finance", "finance_analyst")
    [(statement, params)] = cursor.statements
    assert "pg_auth_members" in statement and "inherit_option" in statement
    assert params == ("finance", "finance_analyst")


def test_snowflake_counts_only_a_usage_grant_of_a_role_as_held():
    adapter, _ = snowflake([("OWNERSHIP", "ROLE", "FINANCE_ANALYST")])
    assert not adapter.holds_role("finance", "finance_analyst")
    assert adapter.granted_roles("finance") == set()
    adapter, _ = snowflake([("USAGE", "ROLE", "FINANCE_ANALYST"), ("USAGE", "SCHEMA", "D.X")])
    assert adapter.granted_roles("finance") == {"FINANCE_ANALYST"}


def test_snowflake_reads_no_hierarchy_it_cannot_see():
    adapter, cursor = snowflake([])
    assert adapter.bound_role_problem("finance_analyst", {"analyst"}) is None
    assert cursor.statements == []


def test_postgres_lists_every_role_granted_to_a_group_role():
    adapter, cursor = postgres([("finance_analyst",), ("pg_read_all_data",)])
    assert adapter.granted_roles("finance") == {"finance_analyst", "pg_read_all_data"}
    [(statement, params)] = cursor.statements
    assert "pg_auth_members" in statement and params == ("finance",)


REACH = [
    # (role, superuser, bypassrls, createrole, createdb, replication)
    ("wrapper", False, False, False, False, False),
]


@pytest.mark.parametrize(
    ("reach", "problem"),
    [
        (REACH, None),
        ([], "no role named"),
        ([*REACH, ("pg_read_all_data", False, False, False, False, False)], "pg_read_all_data"),
        ([*REACH, ("rds_superuser", False, False, False, False, False)], "rds_superuser"),
        ([*REACH, ("azure_pg_admin", False, False, False, False, False)], "azure_pg_admin"),
        ([*REACH, ("cloudsqlsuperuser", False, False, False, False, False)], "cloudsqlsuperuser"),
        ([*REACH, ("analyst", False, False, False, False, False)], "analyst"),
        ([("wrapper", True, False, False, False, False)], "SUPERUSER"),
        ([("wrapper", False, True, False, False, False)], "BYPASSRLS"),
        ([("wrapper", False, False, True, False, False)], "CREATEROLE"),
        ([("wrapper", False, False, False, True, False)], "CREATEDB"),
        ([("wrapper", False, False, False, False, True)], "REPLICATION"),
    ],
)
def test_postgres_refuses_a_bound_role_reaching_privilege_or_a_managed_role(reach, problem):
    adapter, cursor = postgres(reach)
    found = adapter.bound_role_problem("wrapper", {"analyst", "finance"})
    if problem is None:
        assert found is None
    else:
        assert problem in found
    [(statement, params)] = cursor.statements
    assert "WITH RECURSIVE" in statement and params == ("wrapper",)


def test_postgres_refuses_a_mixed_case_bound_role_before_reading():
    adapter, cursor = postgres(REACH)
    assert "lower case" in adapter.bound_role_problem("Finance_Analyst", set())
    assert cursor.statements == []


def test_snowflake_scope_takes_usage_on_a_group_role_only_while_no_role_is_bound():
    registry = {"finance": {"kind": "group"}}
    for privilege, bound, accepted in (
        ("OWNERSHIP", True, True),
        ("USAGE", False, True),
        ("USAGE", True, False),
    ):
        adapter, _ = snowflake([(privilege, "ROLE", "FINANCE")])
        if accepted:
            adapter.check_scope(registry, bound=bound)
        else:
            with pytest.raises(ValueError, match="USAGE on FINANCE"):
                adapter.check_scope(registry, bound=bound)


def test_a_shared_account_binds_to_and_creates_the_groups_suffixed_role():
    adapter, cursor = snowflake([("USAGE", "ROLE", "FINANCE_ANALYST")], shared_account=True)
    assert adapter.bind_statement("finance_analyst", "finance") == (
        'GRANT ROLE "FINANCE_ANALYST" TO ROLE "FINANCE_ACME_DEV";'
    )
    assert adapter.bind_statement("finance_analyst", "finance", grant=False) == (
        'REVOKE ROLE "FINANCE_ANALYST" FROM ROLE "FINANCE_ACME_DEV";'
    )
    assert adapter.holds_role("finance", "finance_analyst")
    assert cursor.statements[-1] == ('SHOW GRANTS TO ROLE "FINANCE_ACME_DEV"', None)
    adapter.create_group("finance")
    assert cursor.statements[-1] == ('CREATE ROLE "FINANCE_ACME_DEV"', None)


def test_a_shared_account_scope_takes_usage_on_its_analyst_role_only_while_a_role_is_bound():
    registry = {"finance": {"kind": "group"}}
    for role, accepted in (("ANALYST_ACME_DEV", True), ("FINANCE_ACME_DEV", False)):
        adapter, _ = snowflake([("USAGE", "ROLE", role)], shared_account=True)
        # Who holds the environment's roles is checked apart; this reads the scope alone.
        adapter._check_holders = lambda cursor, owned: None
        if accepted:
            adapter.check_scope(registry, bound=True)
        else:
            with pytest.raises(ValueError, match=f"USAGE on {role}"):
                adapter.check_scope(registry, bound=True)


def test_postgres_scope_takes_the_bound_flag():
    import inspect

    assert "bound" in inspect.signature(PostgresAdapter.check_scope).parameters
