import json

import pytest

from vecten_shared.osi import OSI_VERSION
from vecten_shared.osi.extensions import vecten_extension


def expression(text):
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": text}]}


@pytest.fixture
def model_dir(tmp_path):
    fields = [
        {"name": "amount", "expression": expression("amount")},
        {
            "name": "country",
            "expression": expression("UPPER(country_code)"),
            "dimension": {},
            "ai_context": {"synonyms": ["market"]},
        },
        {"name": "status", "expression": expression("status"), "dimension": {}},
        {
            "name": "ordered_at",
            "expression": expression("ordered_at"),
            "dimension": {"is_time": True},
        },
        {"name": "email", "expression": expression("email"), "dimension": {}},
    ]
    data = {
        "version": OSI_VERSION,
        "semantic_model": [
            {
                "name": "sales",
                "datasets": [
                    {"name": "orders", "source": "warehouse.marts.orders", "fields": fields},
                    {
                        "name": "customers",
                        "source": "warehouse.marts.customers",
                        "fields": [
                            {"name": "email", "expression": expression("email"), "dimension": {}},
                            {
                                "name": "segment",
                                "expression": expression("segment"),
                                "dimension": {},
                            },
                        ],
                    },
                ],
                "metrics": [
                    {"name": "revenue", "expression": expression("SUM(orders.amount)")},
                    {"name": "average", "expression": expression("AVG(orders.amount)")},
                ],
                "relationships": [
                    {
                        "name": "buyer",
                        "from": "orders",
                        "to": "customers",
                        "from_columns": ["email"],
                        "to_columns": ["email"],
                    }
                ],
            }
        ],
    }
    (tmp_path / "model.json").write_text(json.dumps(data))
    return tmp_path


def load(path, kind="postgres"):
    from vecten_shared.osi.semantic import SemanticModel

    return SemanticModel.load(path, kind=kind, commit="abc123")


def change(path, callback):
    file = path / "model.json"
    data = json.loads(file.read_text())
    callback(data["semantic_model"][0])
    file.write_text(json.dumps(data))


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
def test_parameterized_single_dataset_query(model_dir, kind):
    model = load(model_dir, kind)
    result = model.compile(
        {
            "metrics": ["revenue"],
            "dimensions": ["country"],
            "time": {"grain": "month", "start": "2026-01-01", "end": "2027-01-01"},
            "filters": [
                {"field": "status", "op": "in", "value": ["paid", "x'); DROP TABLE orders;--"]}
            ],
            "order_by": [{"field": "revenue", "dir": "desc"}],
            "limit": 500,
        },
        row_cap=100,
    )
    assert result["resolution"] == "compiled", result
    assert "UPPER(country_code)" in result["sql"]
    assert "SUM(amount)" in result["sql"]
    assert "DATE_TRUNC" in result["sql"] and "LIMIT 100" in result["sql"]
    assert "DROP TABLE" not in result["sql"] and "2026-01-01" not in result["sql"]
    assert result["parameters"] == ["2026-01-01", "2027-01-01", "paid", "x'); DROP TABLE orders;--"]
    assert result["definitions"]["metrics"]["revenue"]["verified"] is None
    assert result["operations"]["revenue"]["additivity"] == "additive"
    assert result["operations"]["revenue"]["rows_may"] == [
        "filter",
        "sort",
        "sum_across_dimensions",
    ]


def test_denied_fields_hidden_and_refused_without_suggestion_leaks(model_dir):
    model = load(model_dir)
    denied = {"orders.email", "orders.amount"}
    described = model.describe("orders", denied=denied)
    assert {f["name"] for f in described["datasets"][0]["fields"]} == {
        "country",
        "status",
        "ordered_at",
    }
    assert not described["metrics"]
    assert not described["relationships"]
    assert not model.search("email", denied={"orders.email", "customers.email"})
    assert model.search("market")[0]["name"] == "country"
    for request in ({"metrics": ["revenue"]}, {"metrics": ["average"], "dimensions": ["email"]}):
        result = model.compile(request, denied=denied)
        assert result["reason"] == "unknown_field"
        assert result["sql"] is None


@pytest.mark.parametrize(
    "query,reason",
    [
        ({"metrics": ["reveneu"]}, "unknown_metric"),
        ({"metrics": ["revenue"], "dimensions": ["customers.segment"]}, "join_required"),
        ({"metrics": ["revenue"], "dimensions": ["missing"]}, "unknown_field"),
    ],
)
def test_structured_refusals(model_dir, query, reason):
    result = load(model_dir).compile(query)
    assert result["resolution"] == "refused" and result["reason"] == reason
    if reason == "join_required":
        assert "JOIN" in result["skeleton"] and "email" in result["skeleton"]
    if reason == "unknown_metric":
        assert "revenue" in result["context"]["suggestions"]


@pytest.mark.parametrize(
    "fragment",
    [
        {"time": {"grain": "hour"}},
        {"filters": [{"field": "status", "op": "regex", "value": "x"}]},
        {"order_by": [{"field": "revenue", "dir": "desc; DROP TABLE t"}]},
        {"limit": -1},
    ],
)
def test_invalid_request_shapes(model_dir, fragment):
    with pytest.raises(ValueError):
        load(model_dir).compile({"metrics": ["revenue"], **fragment})


def test_derived_source_and_nonadditive_metric(model_dir):
    change(
        model_dir,
        lambda m: m["datasets"][0].update(source="SELECT * FROM marts.orders WHERE amount > 0"),
    )
    result = load(model_dir).compile({"metrics": ["average"]})
    assert "FROM (SELECT" in result["sql"]
    assert result["operations"]["average"]["rows_may"] == ["filter", "sort"]


@pytest.mark.parametrize(
    "unsafe",
    ["pg_read_file('/etc/passwd')", "(SELECT amount FROM private)", "amount; DELETE FROM orders"],
)
def test_validator_rejects_unsafe_field_expressions(model_dir, unsafe):
    from vecten_shared.osi.validate import validate_osi

    change(model_dir, lambda m: m["datasets"][0]["fields"][0].update(expression=expression(unsafe)))
    report = validate_osi(model_dir, warehouse_kind="postgres")
    assert not report.ok
    assert any(e.code == "expression_invalid" for e in report.errors)


def test_semiadditive_rollup_requires_rule(model_dir):
    def update(m):
        m["datasets"][0]["custom_extensions"] = [vecten_extension({"grain": "month"})]
        m["metrics"][0]["custom_extensions"] = [
            vecten_extension({"additivity": "semi_additive", "non_additive_over": "time"})
        ]

    change(model_dir, update)
    result = load(model_dir).compile({"metrics": ["revenue"], "time": {"grain": "quarter"}})
    assert result["reason"] == "rollup_not_allowed"
    change(
        model_dir,
        lambda m: m["metrics"][0].update(
            custom_extensions=[
                vecten_extension(
                    {"additivity": "semi_additive", "non_additive_over": "time", "rollup": "last"}
                )
            ]
        ),
    )
    result = load(model_dir).compile({"metrics": ["revenue"], "time": {"grain": "quarter"}})
    assert result["resolution"] == "compiled"
    assert "ROW_NUMBER() OVER" in result["sql"] and "DESC" in result["sql"]


@pytest.mark.docker
def test_semiadditive_last_and_average_execute_correctly(model_dir, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "semantic_docker_util", Path(__file__).with_name("docker_util.py")
    )
    util = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(util)
    docker_available, pg_container = util.docker_available, util.pg_container

    if not docker_available():
        pytest.skip("Docker unavailable")

    def update(model):
        model["datasets"][0]["source"] = "marts.orders"
        model["datasets"][0]["custom_extensions"] = [vecten_extension({"grain": "month"})]
        model["metrics"][0]["custom_extensions"] = [
            vecten_extension(
                {"additivity": "semi_additive", "non_additive_over": "time", "rollup": "last"}
            )
        ]

    change(model_dir, update)
    init = tmp_path / "init"
    init.mkdir(mode=0o755)
    with pg_container(init, {"POSTGRES_PASSWORD": "fixture-only"}) as (connect, _):
        with connect("postgres", "fixture-only", "postgres") as conn:
            conn.execute("CREATE SCHEMA marts")
            conn.execute(
                "CREATE TABLE marts.orders(amount numeric, country_code text, ordered_at timestamp)"
            )
            conn.execute(
                "INSERT INTO marts.orders VALUES (10,'pl','2026-01-01'),"
                "(20,'pl','2026-01-02'),(40,'pl','2026-02-01')"
            )
            compiled = load(model_dir).compile(
                {
                    "metrics": ["revenue", "average"],
                    "dimensions": ["country"],
                    "time": {"grain": "quarter", "start": "2026-01-01", "end": "2027-01-01"},
                }
            )
            rows = conn.execute(compiled["sql"], compiled["parameters"]).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "PL"
            assert rows[0][2] == 40
            assert float(rows[0][3]) == pytest.approx(70 / 3)


def test_case_insensitive_metric_references_cannot_bypass_denials(model_dir):
    change(model_dir, lambda m: m["metrics"][0].update(expression=expression("SUM(ORDERS.AMOUNT)")))
    model = load(model_dir)
    assert not model.describe("orders", denied={"orders.amount"})["metrics"]
    assert (
        model.compile({"metrics": ["revenue"]}, denied={"orders.amount"})["reason"]
        == "unknown_field"
    )
    assert model.compile({"metrics": ["revenue"]})["resolution"] == "compiled"


def test_unqualified_foreign_dimension_reports_join(model_dir):
    result = load(model_dir).compile({"metrics": ["revenue"], "dimensions": ["segment"]})
    assert result["reason"] == "join_required"


@pytest.mark.parametrize(
    "source",
    [
        "marts.orders; DROP TABLE marts.orders",
        "WITH deleted AS (DELETE FROM marts.orders RETURNING *) SELECT * FROM deleted",
    ],
)
def test_unsafe_source_rejected_by_shared_validator(model_dir, source):
    from vecten_shared.osi.validate import validate_osi

    change(model_dir, lambda m: m["datasets"][0].update(source=source))
    assert not validate_osi(model_dir, warehouse_kind="postgres").ok


@pytest.mark.parametrize(
    "sql, escaped",
    [
        ("SELECT 101 % 100 WHERE 1 = %s", "SELECT 101 %% 100 WHERE 1 = %s"),
        ("SELECT 'P%' WHERE a LIKE %s", "SELECT 'P%%' WHERE a LIKE %s"),
        ("SELECT 'it''s %s' WHERE a = %s", "SELECT 'it''s %%s' WHERE a = %s"),
        ('SELECT "100%" FROM t WHERE a = %s', 'SELECT "100%%" FROM t WHERE a = %s'),
        ("SELECT 1 /* 5% */ WHERE a = %s -- 9%\n", "SELECT 1 /* 5%% */ WHERE a = %s -- 9%%\n"),
    ],
)
def test_literal_percents_are_escaped_for_pyformat_drivers(sql, escaped):
    from vecten_shared.osi.compiler import pyformat_statement

    assert pyformat_statement(sql) == escaped


def test_compiled_modulo_metric_escapes_with_bound_values(model_dir):
    from vecten_shared.osi.compiler import pyformat_statement

    def modulo(model):
        model["metrics"].append(
            {"name": "cents", "expression": expression("SUM(orders.amount % 100)")}
        )

    change(model_dir, modulo)
    compiled = load(model_dir).compile(
        {"metrics": ["cents"], "filters": [{"field": "status", "op": "=", "value": "paid"}]}
    )
    statement = pyformat_statement(compiled["sql"])
    assert "%% 100" in statement
    assert statement.replace("%%", "").count("%s") == len(compiled["parameters"])
