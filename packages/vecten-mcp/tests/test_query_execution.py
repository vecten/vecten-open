from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def fixture(kind="postgres"):
    from vecten_mcp.queries import QueryService

    conn = MagicMock()
    cursor = MagicMock()
    cursor.description = [("amount",)]
    cursor.fetchmany.return_value = [(7,), (8,)]
    conn.execute.return_value = cursor
    conn.cursor.return_value = cursor
    opened = []

    @contextmanager
    def acquire(principal, session, expires, roles):
        opened.append((principal, session, expires, roles))
        yield conn

    state = SimpleNamespace(
        get=lambda *args: None, group=lambda group: {"warehouse_role": "finance_role"}
    )
    query = QueryService(state, SimpleNamespace(acquire=acquire), kind=kind)
    caller = SimpleNamespace(
        principal={"id": "p", "warehouse_name": "jane"},
        session_id="s",
        expires_at=1000,
        groups={"finance"},
    )
    return query, conn, cursor, opened, caller


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_queries_are_principal_bound_capped_and_prefixed_with_receipt(kind):
    from vecten_mcp.audit import receipt

    query, conn, cursor, opened, caller = fixture(kind)
    stamp = receipt("abc")
    result = query.execute(caller, "SELECT amount FROM marts.orders", [], stamp)
    assert opened == [(caller.principal, "s", 1000, {"finance_role"})]
    execute = conn.execute if kind == "postgres" else cursor.execute
    query_call = next(
        call for call in execute.call_args_list if str(call.args[0]).startswith("/* vecten")
    )
    assert query_call.args[0].startswith("/* vecten " + stamp["id"] + " */")
    assert "LIMIT 1000" in query_call.args[0]
    assert result["rows"] == [[7], [8]]
    assert result["columns"] == ["amount"]
    assert result["receipt"]["sql_sha256"]
    cursor.fetchmany.assert_called_once_with(1000)
    if kind == "postgres":
        assert any(
            call.args[0] == "SET TRANSACTION READ ONLY" for call in conn.execute.call_args_list
        )
    else:
        assert any(
            "QUERY_TAG" in str(call.args[0]) and stamp["id"] in str(call.args[0])
            for call in cursor.execute.call_args_list
        )


def test_rejected_statements_never_open_a_warehouse_connection():
    from vecten_mcp.audit import receipt

    query, _, _, opened, caller = fixture()
    with pytest.raises(ValueError):
        query.execute(caller, "DELETE FROM marts.orders", [], receipt("abc"))
    assert not opened


def test_parameters_travel_separately_and_receipt_hashes_the_bound_statement():
    from vecten_mcp.audit import bound_sql_hash, receipt

    query, conn, _, _, caller = fixture()
    result = query.execute(
        caller, "SELECT %s AS amount", ["x'; DELETE FROM orders; --"], receipt("abc")
    )
    call = next(
        call for call in conn.execute.call_args_list if str(call.args[0]).startswith("/* vecten")
    )
    assert "DELETE" not in call.args[0]
    assert call.args[1] == ["x'; DELETE FROM orders; --"]
    assert result["receipt"]["sql_sha256"] == bound_sql_hash(result["sql"])


def sent_statement(conn):
    return next(
        call.args[0]
        for call in conn.execute.call_args_list
        if str(call.args[0]).startswith("/* vecten")
    )


def test_postgres_escapes_literal_percents_only_when_values_are_bound():
    from vecten_mcp.audit import receipt

    query, conn, _, _, caller = fixture("postgres")
    sql = "SELECT amount % 100 FROM marts.orders WHERE note LIKE 'a%' AND amount > %s"
    query.execute(caller, sql, [1], receipt("abc"))
    sent = sent_statement(conn)
    assert "amount %% 100" in sent and "'a%%'" in sent
    assert sent.replace("%%", "").count("%s") == 1
    conn.execute.reset_mock()
    query.execute(caller, "SELECT 'a%' AS pattern", [], receipt("abc"))
    assert "'a%'" in sent_statement(conn)
