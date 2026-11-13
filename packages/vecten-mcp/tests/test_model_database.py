"""One committed `osi/` serves every environment: a source naming the project's database is
read as the environment's own."""

import json

import pytest

from vecten_mcp.model import ModelService, read_database
from vecten_mcp.state import StateStore

SOURCES = {
    "orders": "ACME.MARTS.ORDERS",
    "accounts": "acme.core.accounts",
    "shipped": '"ACME"."MARTS"."SHIPPED"',
    "partner": "PARTNER_DB.PUBLIC.FEED",
    "local": "MARTS.LOCAL_ONLY",
    "joined": "(SELECT o.id FROM ACME.MARTS.ORDERS AS o JOIN PARTNER_DB.PUBLIC.FEED AS f "
    "ON o.id = f.id)",
}


def _field(name):
    return {"name": name, "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]}}


@pytest.fixture
def osi(tmp_path):
    directory = tmp_path / "osi"
    directory.mkdir()
    datasets = [
        {"name": name, "source": source, "fields": [_field("id")]}
        for name, source in SOURCES.items()
    ]
    datasets[0]["fields"].append(_field("amount"))
    metrics = [
        {
            "name": "revenue",
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
        }
    ]
    document = {
        "version": "0.1.1",
        "semantic_model": [{"name": "m", "datasets": datasets, "metrics": metrics}],
    }
    (directory / "datasets.json").write_text(json.dumps(document))
    return directory


def load(osi, database, swapped=None):
    checked = []

    def exists(table):
        checked.append(table.sql(dialect="snowflake"))
        return True

    model = ModelService(
        osi,
        StateStore("sqlite://"),
        kind="snowflake",
        source_exists=exists,
        database=("ACME", database),
        on_swap=(lambda model: swapped.append(model)) if swapped is not None else None,
    )
    assert model.reload(), model.health()
    return model, checked


def test_compiled_sql_describe_and_grants_name_the_environments_database(osi):
    swapped = []
    dev, _ = load(osi, "ACME_DEV", swapped)
    compiled = dev.current.compile({"metrics": ["revenue"]})
    assert compiled["resolution"] == "compiled"
    assert "ACME_DEV" in compiled["sql"] and "ACME." not in compiled["sql"]
    described = {d["name"]: d["source"] for d in dev.current.describe()["datasets"]}
    assert described["orders"] == "ACME_DEV.MARTS.ORDERS"
    # The access service grants on the sources the swap hands it.
    (model,) = swapped
    assert model.datasets["orders"]["source"] == "ACME_DEV.MARTS.ORDERS"


def test_the_adapter_finds_no_source_in_another_environments_database(tmp_path):
    from vecten_mcp.warehouse.snowflake import SnowflakeAdapter
    from vecten_shared.config import VectenConfig
    from vecten_shared.provider import snowflake_names

    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "acme", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "account": "ACME-SHARED", "shared_account": True},
        }
    )
    calls = []

    class Connection:
        def cursor(self):
            return self

        def execute(self, query, parameters=None):
            calls.append(parameters)
            return self

        def fetchone(self):
            return (True,)

        def close(self):
            pass

    adapter = SnowflakeAdapter(Connection, None, None, names=snowflake_names(config, "dev"))
    directory = tmp_path / "osi"
    directory.mkdir()
    datasets = [
        {"name": "orders", "source": "ACME.MARTS.ORDERS", "fields": [_field("id")]},
        {"name": "theirs", "source": "ACME_PROD.MARTS.ORDERS", "fields": [_field("id")]},
    ]
    (directory / "datasets.json").write_text(
        json.dumps({"version": "0.1.1", "semantic_model": [{"name": "m", "datasets": datasets}]})
    )
    model = ModelService(
        directory,
        StateStore("sqlite://"),
        kind="snowflake",
        source_exists=adapter.source_exists,
        database=("ACME", "ACME_DEV"),
    )
    assert not model.reload()
    errors = model.health()["errors"]
    assert [(e["code"], e["dataset"]) for e in errors] == [("source_missing", "theirs")]
    # Only dev's own source reached Snowflake.
    assert calls == [("MARTS", "ORDERS")]


def test_two_environments_of_a_shared_account_read_their_own_database(osi):
    dev, dev_checked = load(osi, "ACME_DEV")
    prod, prod_checked = load(osi, "ACME_PROD")
    assert dev.current.datasets["orders"]["source"] == "ACME_DEV.MARTS.ORDERS"
    assert prod.current.datasets["orders"]["source"] == "ACME_PROD.MARTS.ORDERS"
    assert dev.current.datasets["accounts"]["source"] == "ACME_DEV.core.accounts"
    assert dev.current.datasets["shipped"]["source"] == '"ACME_DEV"."MARTS"."SHIPPED"'
    assert "ACME_DEV.MARTS.ORDERS" in dev_checked and "ACME_PROD.MARTS.ORDERS" in prod_checked
    assert not any(table.startswith("ACME.") for table in dev_checked + prod_checked)
    joined = dev.current.datasets["joined"]["source"]
    assert "ACME_DEV.MARTS.ORDERS" in joined and "PARTNER_DB.PUBLIC.FEED" in joined


def test_a_source_naming_another_database_or_none_is_read_as_written(osi):
    dev, _ = load(osi, "ACME_DEV")
    assert dev.current.datasets["partner"]["source"] == SOURCES["partner"]
    assert dev.current.datasets["local"]["source"] == SOURCES["local"]


def test_fixed_names_read_every_source_as_written(osi):
    fixed, _ = load(osi, "ACME")
    assert {n: d["source"] for n, d in fixed.current.datasets.items()} == SOURCES


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("ACME.MARTS.ORDERS", "ACME_DEV.MARTS.ORDERS"),
        ('"acme".MARTS.ORDERS', '"acme".MARTS.ORDERS'),
        ("ACMEX.MARTS.ORDERS", "ACMEX.MARTS.ORDERS"),
    ],
)
def test_only_the_projects_database_is_read_as_the_environments(source, expected):
    # A quoted lower-case name is another database than the unquoted ACME.
    assert read_database(source, "snowflake", "ACME", "ACME_DEV") == expected
