import json

import pytest

from vecten_shared.osi import OSI_VERSION
from vecten_shared.osi.expressions import FUNCTIONS, parse_expression
from vecten_shared.osi.extensions import vecten_extension
from vecten_shared.osi.semantic import SemanticModel


def expr(text, dialect="ANSI_SQL"):
    return {"dialects": [{"dialect": dialect, "expression": text}]}


def write(tmp_path, fields, metrics):
    doc = {
        "version": OSI_VERSION,
        "semantic_model": [
            {
                "name": "m",
                "datasets": [
                    {
                        "name": "orders",
                        "source": "warehouse.marts.orders",
                        "primary_key": ["order_id"],
                        "fields": fields,
                    }
                ],
                "metrics": metrics,
            }
        ],
    }
    (tmp_path / "model.json").write_text(json.dumps(doc))
    return tmp_path


BASE = [
    {"name": "order_id", "expression": expr("order_id"), "dimension": {}},
    {"name": "order_date", "expression": expr("order_date"), "dimension": {"is_time": True}},
    {"name": "country", "expression": expr("country"), "dimension": {}},
    {"name": "total_amount", "expression": expr("total_amount")},
    {"name": "product_amount", "expression": expr("product_amount")},
    {"name": "balance", "expression": expr("balance")},
    {"name": "label", "expression": expr("CAST(country AS TEXT)"), "dimension": {}},
]


def test_three_fields_three_classes(tmp_path):
    model = SemanticModel.load(write(tmp_path, BASE, []))
    fields = model.fields("orders")
    assert fields["order_date"]["classification"] == "time_dimension"
    assert fields["country"]["classification"] == "dimension"
    assert fields["total_amount"]["classification"] == "fact"


def test_additivity_inferred_and_declared(tmp_path):
    metrics = [
        {"name": "total_revenue", "expression": expr("SUM(orders.total_amount)")},
        {"name": "orders_count", "expression": expr("COUNT(*)")},
        {
            "name": "product_share",
            "expression": expr("SUM(orders.product_amount) / SUM(orders.total_amount)"),
        },
        {
            "name": "median_order",
            "expression": expr("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY orders.total_amount)"),
        },
        {
            "name": "account_balance",
            "expression": expr("SUM(orders.balance)"),
            "custom_extensions": [
                vecten_extension(
                    {"additivity": "semi_additive", "non_additive_over": "time", "rollup": "last"}
                )
            ],
        },
    ]
    model = SemanticModel.load(write(tmp_path, BASE, metrics))
    got = {n: m["additivity"] for n, m in model.metrics.items()}
    assert got == {
        "total_revenue": "additive",
        "orders_count": "additive",
        "product_share": "non_additive",
        "median_order": "non_additive",
        "account_balance": "semi_additive",
    }
    assert model.metrics["account_balance"]["additivity_rule"] == {
        "non_additive_over": "time",
        "rollup": "last",
    }
    assert "additivity_rule" not in model.metrics["total_revenue"]


def test_snowflake_transpiles_the_ansi_expression(tmp_path):
    model = SemanticModel.load(write(tmp_path, BASE, []), kind="snowflake")
    assert model.fields("orders")["label"]["expression_sql"] == "CAST(country AS VARCHAR)"
    pg = SemanticModel.load(tmp_path, kind="postgres")
    assert pg.fields("orders")["label"]["expression_sql"] == "CAST(country AS TEXT)"


def test_snowflake_entry_wins_on_snowflake_and_is_ignored_on_postgres(tmp_path):
    code = {
        "name": "code",
        "expression": {
            "dialects": [
                {"dialect": "ANSI_SQL", "expression": "UPPER(country)"},
                {"dialect": "SNOWFLAKE", "expression": "LOWER(country)"},
            ]
        },
        "dimension": {},
    }
    write(tmp_path, [*BASE, code], [])
    snowflake = SemanticModel.load(tmp_path, kind="snowflake").fields("orders")["code"]
    postgres = SemanticModel.load(tmp_path, kind="postgres").fields("orders")["code"]
    assert snowflake["expression_sql"] == "LOWER(country)"
    assert postgres["expression_sql"] == "UPPER(country)"


def test_metric_records_the_expression_it_runs(tmp_path):
    metrics = [{"name": "revenue", "expression": expr("SUM(orders.total_amount)")}]
    model = SemanticModel.load(write(tmp_path, BASE, metrics), kind="snowflake")
    assert model.metrics["revenue"]["expression_sql"] == "SUM(orders.total_amount)"


def test_allow_list_names_the_rejected_construct():
    with pytest.raises(ValueError, match="Subquery|Select"):
        parse_expression("(SELECT 1)")
    with pytest.raises(ValueError, match="PG_SLEEP"):
        parse_expression("PG_SLEEP(1)")
    assert {"PERCENTILE_CONT", "PERCENTILE_DISC"} <= FUNCTIONS
