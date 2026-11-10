import pytest


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT 1; SELECT 2",
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "SELECT * INTO copy FROM orders",
        "EXPLAIN ANALYZE DELETE FROM orders",
        "EXPLAIN ANALYZE SELECT amount FROM marts.orders",
        "EXPLAIN (ANALYZE TRUE, FORMAT JSON) SELECT amount FROM marts.orders",
        "EXPLAIN (ANALYZE TRUE, FORMAT JSON) UPDATE orders SET amount=0",
        "SELECT * FROM orders FOR UPDATE",
        "CALL arbitrary()",
        "SHOW ALL; DROP TABLE orders",
        "SELECT set_config('role', 'other_person', false)",
        "SELECT pg_catalog.set_config('role', 'other_person', false)",
        "",
    ],
)
def test_write_and_multiple_statements_are_rejected(sql):
    from vecten_shared.osi.sql_policy import read_statement

    with pytest.raises(ValueError):
        read_statement(sql, kind="postgres", row_cap=20)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT amount FROM marts.orders",
        "WITH x AS (SELECT amount FROM marts.orders) SELECT * FROM x",
        "EXPLAIN SELECT amount FROM marts.orders",
        "EXPLAIN (FORMAT JSON) SELECT amount FROM marts.orders",
        "SHOW ALL",
        "DESCRIBE orders",
    ],
)
def test_read_statements_are_bounded(sql):
    from vecten_shared.osi.sql_policy import read_statement

    result = read_statement(sql, kind="postgres", row_cap=20)
    if sql.startswith(("SELECT", "WITH")):
        assert "LIMIT 20" in result.sql
    if "marts.orders" in sql:
        assert "marts.orders" in result.tables
        assert "amount" in result.columns
