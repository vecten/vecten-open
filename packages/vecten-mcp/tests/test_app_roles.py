"""An app principal queries with the roles of its app's admitted groups only."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.apps import ADMISSION, APP_PRINCIPALS, AppScopes, BoundApp
from vecten_mcp.identity import Identity
from vecten_mcp.queries import QueryService
from vecten_mcp.runtime_setup import session_validator
from vecten_mcp.state import StateStore
from vecten_mcp.warehouse.pool import PrincipalPools

APP = BoundApp("acme-sales", "overview", "Sales", "container", ("orders",), ("a", "b"))


class Adapter:
    def create_group(self, role):
        pass

    def membership(self, role, name, grant=True):
        pass


def scopes():
    state = StateStore("sqlite://")
    access = AccessService(state, Adapter())
    for group in ("a", "b", "c"):
        access.create_group(group)
    principal = state.create_principal(
        Identity("service", "svc_app_overview", "x@service.invalid", "App"),
        "svc_app_overview",
        kind="service",
        provisioned=True,
    )
    apps = AppScopes(state, {"overview": APP}, kind="postgres")
    state.put(ADMISSION, APP.key, {"admitted": ["a"], "excluded": [], "checked_at": 0})
    return state, apps, principal


def test_an_app_principal_queries_through_its_admitted_groups_only():
    state, apps, principal = scopes()
    apps.mark(principal["id"], "acme-sales", "overview")
    assert apps.query_groups(principal["id"], {"a", "b", "c", "default"}) == {"a"}


def test_any_other_principal_keeps_its_groups():
    state, apps, principal = scopes()
    assert apps.query_groups(principal["id"], {"a", "b"}) is None


def test_a_principal_marked_for_an_unbound_app_queries_through_no_group():
    state, apps, principal = scopes()
    state.put(APP_PRINCIPALS, principal["id"], {"extension": "gone", "app": "retired"})
    assert apps.query_groups(principal["id"], {"a"}) == set()


ROLES = {"a": "role_a", "b": "role_b", "c": "role_c", "finance": "finance_role"}


def query_fixture(kind, narrowed, stray=()):
    conn, cursor = MagicMock(), MagicMock()
    cursor.description = [("amount",)]
    cursor.fetchmany.return_value = [(7,)]
    stray_cursor = MagicMock()
    stray_cursor.fetchall.return_value = [(role,) for role in stray]

    def execute(statement, *args):
        return stray_cursor if "pg_has_role" in str(statement) else cursor

    conn.execute.side_effect = execute
    conn.cursor.return_value = cursor
    opened = []

    @contextmanager
    def acquire(principal, session, expires, roles):
        opened.append(roles)
        yield conn

    state = SimpleNamespace(
        get=lambda *args: None, group=lambda group: {"warehouse_role": ROLES[group]}
    )
    query = QueryService(state, SimpleNamespace(acquire=acquire), kind=kind)
    query.narrow = lambda pid, groups: narrowed
    caller = SimpleNamespace(
        principal={"id": "p", "warehouse_name": "svc_app_overview"},
        session_id="s",
        expires_at=1000,
        groups={"a", "b", "c"},
    )
    return query, conn, opened, caller


def sent(conn):
    return [str(c.args[0]) for c in conn.execute.call_args_list]


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_the_query_opens_with_the_admitted_groups_roles(kind):
    from vecten_mcp.audit import receipt

    query, conn, opened, caller = query_fixture(kind, {"a"})
    query.execute(caller, "SELECT amount FROM marts.orders", [], receipt("abc"))
    assert opened == [{"role_a"}]


def test_on_postgres_a_role_beyond_the_admitted_ones_refuses_the_query():
    from vecten_mcp.audit import receipt

    query, conn, opened, caller = query_fixture("postgres", {"a"}, stray=("role_c",))
    with pytest.raises(ValueError, match="role_c"):
        query.execute(caller, "SELECT amount FROM marts.orders", [], receipt("abc"))
    assert not [s for s in sent(conn) if s.startswith("/* vecten")]
    checks = [c for c in conn.execute.call_args_list if "pg_has_role" in str(c.args[0])]
    assert checks and checks[0].args[1] == (["role_a"],)


def test_on_postgres_any_other_principal_skips_the_role_check():
    from vecten_mcp.audit import receipt

    query, conn, opened, caller = query_fixture("postgres", None)
    query.execute(caller, "SELECT amount FROM marts.orders", [], receipt("abc"))
    assert opened == [{"role_a", "role_b", "role_c"}]
    assert not [s for s in sent(conn) if "pg_has_role" in s]


def test_an_app_that_admits_no_group_reads_nothing():
    from vecten_mcp.audit import receipt

    query, conn, opened, caller = query_fixture("snowflake", set())
    with pytest.raises(ValueError, match="admits no group"):
        query.execute(caller, "SELECT amount FROM marts.orders", [], receipt("abc"))
    assert opened == []


def test_the_pool_validator_narrows_an_app_principals_roles():
    state, apps, principal = scopes()
    apps.mark(principal["id"], "acme-sales", "overview")
    from sqlalchemy import insert

    from vecten_mcp.state import memberships

    with state.transaction() as conn:
        for group in ("a", "b", "c"):
            conn.execute(insert(memberships).values(principal_id=principal["id"], group_name=group))
    state.put("sessions", "s1", {"principal_id": principal["id"]})
    validate = session_validator(state, lambda: 0, narrow=apps.query_groups)
    _, roles = validate(principal, "s1", {"anything"})
    assert roles == {"a"}


def test_the_pool_opens_a_new_connection_when_the_roles_change():
    opened = []

    class Warehouse:
        def open_membership(self, name):
            pass

        def connect_principal(self, principal, roles, expires_at):
            connection = MagicMock()
            opened.append((connection, frozenset(roles)))
            return connection

    pools = PrincipalPools(Warehouse(), clock=lambda: 0)
    principal = {
        "id": "p",
        "warehouse_name": "svc_app_overview",
        "enabled": True,
        "provisioned": True,
    }
    with pools.acquire(principal, "s", 100, {"role_a", "role_b"}) as first:
        pass
    with pools.acquire(principal, "s", 100, {"role_a", "role_b"}) as again:
        assert again is first
    with pools.acquire(principal, "s", 100, {"role_a"}) as narrowed:
        assert narrowed is not first
    assert [roles for _, roles in opened] == [{"role_a", "role_b"}, {"role_a"}]
    first.close.assert_called_once()
