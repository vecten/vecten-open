"""Every name Vecten gives an object in Snowflake, from one function."""

from dataclasses import astuple

import pytest

from vecten_shared.config import VectenConfig
from vecten_shared.provider import SnowflakeNames, snowflake_names


def config(name, database=None, account="ACME-PROD"):
    return VectenConfig.model_validate(
        {
            "users": 3,
            "project": {"name": name, "scaffold_version": "0.1.0"},
            "warehouse": {"kind": "snowflake", "account": account, "database": database},
        }
    )


# The names running environments carry on release/0.1.1, spelled out so that no edit to the
# function can rename one: (project, [warehouse] database, env) -> (database, warehouse).
RELEASED = [
    ("acme", None, "prod", "ACME", "ACME_PROD_WH"),
    ("acme", None, "dev", "ACME", "ACME_DEV_WH"),
    ("acme", "analytics", "prod", "ANALYTICS", "ACME_PROD_WH"),
    ("acme-co", None, "staging", "ACME_CO", "ACME_CO_STAGING_WH"),
    ("acme-co", "analytics", "prod-2", "ANALYTICS", "ACME_CO_PROD_2_WH"),
    ("data_team", "Lake", "eu1", "LAKE", "DATA_TEAM_EU1_WH"),
]


@pytest.mark.parametrize(("project", "database", "env", "db_name", "wh_name"), RELEASED)
def test_fixed_names_are_the_released_literals(project, database, env, db_name, wh_name):
    names = snowflake_names(config(project, database), env)
    assert names.account == "ACME-PROD"
    assert names.suffix == ""
    assert names.database == db_name
    assert names.warehouse == wh_name
    assert names.analyst_role == "ANALYST"
    assert names.loader_role == "LOADER"
    assert names.loader_user == "LOADER"
    assert names.provisioner_role == "PROVISIONER"
    assert names.provisioner_user == "VECTEN_PROVISIONER"
    assert names.integration == "VECTEN_EXTERNAL_OAUTH"
    assert names.access_schema == "VECTEN_ACCESS"
    assert names.authentication_policy is None
    assert names.audience == "https://ACME-PROD.snowflakecomputing.com"


def test_fixed_is_the_default_layout():
    assert snowflake_names(config("acme"), "prod") == snowflake_names(
        config("acme"), "prod", shared_account=False
    )


def test_names_in_a_shared_account_carry_the_environment_suffix():
    names = snowflake_names(config("acme"), "dev", shared_account=True)
    assert names == SnowflakeNames(
        account="ACME-PROD",
        suffix="_ACME_DEV",
        database="ACME_DEV",
        warehouse="ACME_DEV_WH",
        analyst_role="ANALYST_ACME_DEV",
        loader_role="LOADER_ACME_DEV",
        loader_user="LOADER_ACME_DEV",
        provisioner_role="PROVISIONER_ACME_DEV",
        provisioner_user="PROVISIONER_ACME_DEV",
        integration="EXTERNAL_OAUTH_ACME_DEV",
        access_schema="VECTEN_ACCESS",
        authentication_policy="MCP_SIGN_IN",
    )


def test_a_shared_account_bases_every_name_on_the_configured_database():
    names = snowflake_names(config("acme-co", "data-lake"), "prod", shared_account=True)
    assert names.suffix == "_DATA_LAKE_PROD"
    assert names.database == "DATA_LAKE_PROD"
    assert names.warehouse == "DATA_LAKE_PROD_WH"
    assert names.analyst_role == "ANALYST_DATA_LAKE_PROD"


def test_two_shared_environments_share_no_name():
    dev = snowflake_names(config("acme"), "dev", shared_account=True)
    prod = snowflake_names(config("acme"), "prod", shared_account=True)
    shared = set(astuple(dev)) & set(astuple(prod))
    assert shared == {"ACME-PROD", "VECTEN_ACCESS", "MCP_SIGN_IN"}


def test_the_audience_needs_an_account():
    names = snowflake_names(config("acme", account=None), "prod")
    assert names.account is None
    with pytest.raises(ValueError):
        _ = names.audience
