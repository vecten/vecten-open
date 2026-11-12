"""An app principal's sql-exec reads only its declared datasets' tables, whatever the SQL hides."""

import pytest
from sqlglot import exp

from vecten_mcp.app_sql import FUNCTIONS, relation, sql_problem

POSTGRES = frozenset(
    {relation("marts.orders", "postgres"), relation("marts.customers", "postgres")}
)
SNOWFLAKE = frozenset(
    {relation("marts.orders", "snowflake"), relation("marts.customers", "snowflake")}
)
RELATIONS = {"postgres": POSTGRES, "snowflake": SNOWFLAKE}
BOTH = ["postgres", "snowflake"]


def problem(sql, kind="postgres"):
    return sql_problem(sql, kind=kind, relations=RELATIONS[kind])


@pytest.mark.parametrize("kind", BOTH)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region, sum(amount) FROM marts.orders GROUP BY 1",
        "SELECT o.amount, c.name FROM marts.orders AS o JOIN marts.customers AS c ON o.id = c.id",
        "WITH recent AS (SELECT * FROM marts.orders) SELECT count(*) FROM recent",
        "SELECT * FROM (SELECT amount FROM marts.orders) AS t",
        "SELECT * FROM marts.orders WHERE id IN (SELECT id FROM marts.customers)",
        "SELECT amount FROM marts.orders UNION ALL SELECT amount FROM marts.orders",
        # A CTE may take the name of an undeclared table: the query then reads the CTE.
        "WITH payroll AS (SELECT * FROM marts.orders) SELECT * FROM payroll",
        "WITH a AS (SELECT 1 AS x), b AS (SELECT * FROM a) SELECT * FROM b",
        "WITH RECURSIVE n AS (SELECT 1 AS i UNION ALL SELECT i + 1 FROM n WHERE i < 3) "
        "SELECT * FROM n",
        "SELECT date_trunc('month', created_at), coalesce(sum(amount), 0) FROM marts.orders "
        "GROUP BY 1 ORDER BY 1 LIMIT 10",
    ],
)
def test_declared_tables_ctes_and_subqueries_are_read(sql, kind):
    assert problem(sql, kind) is None


def test_quoted_and_case_folded_names_of_a_declared_table_are_read():
    assert problem('SELECT * FROM "marts"."orders"') is None
    assert problem("SELECT * FROM MARTS.ORDERS") is None
    assert problem('SELECT * FROM "MARTS"."ORDERS"', "snowflake") is None
    assert problem("SELECT * FROM marts.orders", "snowflake") is None


@pytest.mark.parametrize("kind", BOTH)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM marts.payroll",
        "SELECT o.* FROM marts.orders AS o JOIN marts.payroll AS p ON o.id = p.id",
        "WITH p AS (SELECT * FROM marts.payroll) SELECT * FROM p",
        "SELECT * FROM (SELECT * FROM marts.payroll) AS t",
        "SELECT (SELECT max(salary) FROM marts.payroll) FROM marts.orders",
        "SELECT * FROM marts.orders WHERE EXISTS (SELECT 1 FROM marts.payroll)",
        "SELECT amount FROM marts.orders UNION SELECT salary FROM marts.payroll",
        "SELECT * FROM marts.orders, LATERAL (SELECT * FROM marts.payroll) AS p",
        # Only the declared spelling: an unqualified name resolves through the search path.
        "SELECT * FROM orders",
        "SELECT * FROM other_schema.orders",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM information_schema.columns WHERE table_name = 'orders'",
    ],
)
def test_undeclared_relations_are_refused(sql, kind):
    message = problem(sql, kind)
    assert message is not None and "declared" in message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_catalog.pg_class",
        "SELECT * FROM pg_class",
        "SELECT * FROM pg_stat_activity",
        # A quoted name is another relation than the folded declared one.
        'SELECT * FROM "MARTS"."ORDERS"',
        'SELECT * FROM "marts"."payroll"',
    ],
)
def test_postgres_catalogs_and_other_spellings_are_refused(sql):
    assert problem(sql) is not None


@pytest.mark.parametrize(
    "sql",
    [
        # The CTE is defined later, so inside `a` the name `b` is the table b.
        "WITH a AS (SELECT * FROM b), b AS (SELECT * FROM marts.orders) SELECT * FROM a",
        # A quoted CTE name is not the folded name the query reads.
        'WITH "PAYROLL" AS (SELECT * FROM marts.orders) SELECT * FROM PAYROLL',
        # A CTE inside a subquery is not visible outside it.
        "SELECT * FROM (WITH p AS (SELECT * FROM marts.orders) SELECT * FROM p) AS t, p",
        # A schema-qualified name never names a CTE.
        "WITH payroll AS (SELECT * FROM marts.orders) SELECT * FROM marts.payroll",
    ],
)
def test_names_that_only_look_like_ctes_are_tables(sql):
    assert problem(sql) is not None


@pytest.mark.parametrize("kind", BOTH)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM marts.orders -- trailing note",
        "/* leading */ SELECT * FROM marts.orders",
        "SELECT /* inline */ * FROM marts.orders",
        "SELECT 1 -- */ FROM marts.payroll\n",
    ],
)
def test_comments_are_refused(sql, kind):
    assert "comment" in problem(sql, kind)


@pytest.mark.parametrize("kind", BOTH)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM marts.orders; SELECT * FROM marts.payroll",
        "SELECT * FROM marts.orders; DROP TABLE marts.orders",
        "USE SCHEMA marts",
        "USE ROLE accountadmin",
        "SET search_path = marts",
        "SHOW TABLES",
        "DESCRIBE TABLE marts.payroll",
        "EXPLAIN SELECT * FROM marts.orders",
        "DELETE FROM marts.orders",
        "",
    ],
)
def test_only_one_select_query_is_accepted(sql, kind):
    assert problem(sql, kind) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT query_to_xml('select * from marts.payroll', true, false, '')",
        "SELECT * FROM dblink('dbname=warehouse', 'select * from marts.payroll') AS t(a int)",
        "SELECT current_setting('search_path')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT to_regclass('marts.payroll')",
        "SELECT * FROM generate_series(1, 3)",
        "SELECT * FROM ROWS FROM (generate_series(1, 2))",
        "SELECT (NULL::marts.payroll).*",
        "SELECT 'marts.payroll'::regclass",
        "SELECT * FROM marts.orders WHERE id = (SELECT count(*) FROM (TABLE marts.payroll) AS t)",
    ],
)
def test_postgres_functions_and_casts_that_name_relations_are_refused(sql):
    assert problem(sql) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM IDENTIFIER('marts.payroll')",
        "SELECT * FROM TABLE('marts.payroll')",
        "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
        "SELECT * FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY())",
        "SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
        "SELECT $1 FROM @mystage",
        "SELECT GET_DDL('table', 'marts.payroll')",
        "SELECT SYSTEM$GET_PRIVATELINK_CONFIG()",
        "SELECT * FROM analytics.marts.orders",
    ],
)
def test_snowflake_names_in_strings_table_functions_and_stages_are_refused(sql):
    assert problem(sql, "snowflake") is not None


def test_snowflake_flatten_over_a_declared_table_is_read():
    sql = "SELECT f.value FROM marts.orders AS o, LATERAL FLATTEN(input => o.items) AS f"
    assert problem(sql, "snowflake") is None


def test_the_message_names_the_relation_the_query_reads():
    assert "marts.payroll" in problem("SELECT * FROM marts.payroll")
    assert "query_to_xml" in problem("SELECT query_to_xml('select 1', true, false, '')").lower()


def test_a_three_part_declared_source_matches_only_its_three_parts():
    relations = frozenset({relation("analytics.marts.orders", "snowflake")})
    assert (
        sql_problem("SELECT * FROM analytics.marts.orders", kind="snowflake", relations=relations)
        is None
    )
    assert sql_problem("SELECT * FROM marts.orders", kind="snowflake", relations=relations)


def test_no_declared_relation_refuses_every_table():
    assert sql_problem("SELECT * FROM marts.orders", kind="postgres", relations=frozenset())
    assert sql_problem("SELECT 1", kind="postgres", relations=frozenset()) is None


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def test_sqlglot_functions_that_could_name_a_relation_were_reviewed():
    """A SQLGlot upgrade that adds a function class with one of these words fails here, so
    someone decides whether it reads a relation by name; none of them is permitted to apps."""
    words = ("read", "file", "scan", "identifier", "table", "stage", "query", "predict", "url")
    named = {
        cls for cls in _subclasses(exp.Func) if any(word in cls.__name__.lower() for word in words)
    }
    assert {cls.__name__ for cls in named} == {
        "DynamicIdentifier",
        "GenerateTable",
        "JSONTable",
        "ParseUrl",
        "Predict",
        "ReadCSV",
        "ReadParquet",
        "SplitToTable",
        "ToFile",
        "XMLTable",
    }
    for kind in BOTH:
        assert not named & FUNCTIONS[kind]


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT * FROM marts.orders, LATERAL TABLE('marts.payroll')", "snowflake"),
        ("SELECT * FROM marts.orders, LATERAL TABLE(?)", "snowflake"),
        ("SELECT * FROM marts.orders, LATERAL TABLE($v)", "snowflake"),
        ("SELECT * FROM marts.orders CROSS JOIN LATERAL marts.payroll", "postgres"),
        ('SELECT * FROM marts.orders, LATERAL "payroll"', "postgres"),
        ("SELECT * FROM marts.orders, LATERAL payroll", "postgres"),
        ("SELECT * FROM marts.orders, LATERAL marts.payroll", "snowflake"),
    ],
)
def test_only_a_subquery_or_a_row_generating_function_follows_lateral(sql, kind):
    assert problem(sql, kind) is not None


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT * FROM marts.orders AS o, LATERAL (SELECT 1) AS x", "postgres"),
        ("SELECT * FROM marts.orders AS o, LATERAL unnest(o.tags) AS t", "postgres"),
        (
            "SELECT f.value FROM marts.orders AS o, LATERAL FLATTEN(input => o.items) AS f",
            "snowflake",
        ),
    ],
)
def test_lateral_subqueries_and_row_functions_over_declared_tables_are_read(sql, kind):
    assert problem(sql, kind) is None


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        # SQLGlot knows these names; the warehouse would resolve them as functions a schema
        # may define, so they are not on the warehouse's list.
        ("SELECT OBJECT_ID('marts.payroll')", "postgres"),
        ("SELECT CHECK_XML(amount) FROM marts.orders", "snowflake"),
        ("SELECT REG_DOMAIN(region) FROM marts.orders", "postgres"),
        ("SELECT CODE_POINTS_TO_STRING(amount) FROM marts.orders", "snowflake"),
        ("SELECT CURRENT_REGION()", "snowflake"),
        ("SELECT 1 OPERATOR(pg_catalog.+) 2", "postgres"),
        ("SELECT amount OPERATOR(marts.+) 1 FROM marts.orders", "postgres"),
    ],
)
def test_functions_and_operators_off_the_warehouses_list_are_refused(sql, kind):
    assert problem(sql, kind) is not None


@pytest.mark.parametrize("kind", BOTH)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(DISTINCT id), avg(amount), min(amount), max(amount) FROM marts.orders",
        "SELECT CASE WHEN amount > 10 THEN 'big' ELSE 'small' END FROM marts.orders",
        "SELECT CAST(amount AS INT), nullif(region, ''), lower(region), upper(region) "
        "FROM marts.orders",
        "SELECT round(amount, 2), abs(amount), floor(amount), ceil(amount) FROM marts.orders",
        "SELECT concat(region, '-'), length(region), substring(region, 1, 2), trim(region) "
        "FROM marts.orders",
        "SELECT extract(year FROM created_at), current_date FROM marts.orders",
        "SELECT region, row_number() OVER (PARTITION BY region ORDER BY amount DESC) "
        "FROM marts.orders",
        "SELECT lag(amount) OVER (ORDER BY created_at), greatest(amount, 0), least(amount, 1) "
        "FROM marts.orders",
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) FROM marts.orders",
        "SELECT sum(amount) FILTER (WHERE region = 'eu') FROM marts.orders",
    ],
)
def test_common_reporting_functions_are_on_both_lists(sql, kind):
    if "FILTER" in sql and kind == "snowflake":
        pytest.skip("Snowflake has no aggregate FILTER clause")
    assert problem(sql, kind) is None


def test_snowflake_reporting_functions_are_on_its_list():
    for sql in (
        "SELECT iff(amount > 1, 'a', 'b'), dateadd(day, 1, created_at) FROM marts.orders",
        "SELECT datediff(day, created_at, current_date), to_varchar(amount) FROM marts.orders",
    ):
        assert problem(sql, "snowflake") is None, sql


@pytest.mark.parametrize(
    ("sql", "kind"),
    [("SELECT * FROM marts.orders..payroll", "snowflake"), ("SELECT * FROM ((", "postgres")],
)
def test_sql_the_parser_cannot_read_is_a_refusal_not_a_failure(sql, kind):
    assert isinstance(problem(sql, kind), str)
