"""`[warehouse] shared_account` and the layout each environment records."""

import tomllib

import pytest
from pydantic import ValidationError

from vecten_shared.config import VectenConfig, load_config, save_config

PROJECT = {"users": 1, "project": {"name": "acme", "scaffold_version": "0.1.0"}}
SNOWFLAKE = {"kind": "snowflake", "account": "ACME-PROD"}


def test_a_shared_account_is_off_by_default():
    config = VectenConfig.model_validate({**PROJECT, "warehouse": SNOWFLAKE})
    assert config.warehouse.shared_account is False
    assert config.environments.get("prod") is None


def test_a_shared_account_needs_the_platform_component():
    with pytest.raises(ValidationError, match="shared_account needs the platform component"):
        VectenConfig.model_validate(
            {
                **PROJECT,
                "components": ["mcp"],
                "warehouse": {**SNOWFLAKE, "existing": True, "shared_account": True},
            }
        )


def test_a_shared_account_needs_a_snowflake_warehouse():
    with pytest.raises(ValidationError, match="shared_account needs a Snowflake warehouse"):
        VectenConfig.model_validate({**PROJECT, "warehouse": {"shared_account": True}})


def test_the_setting_and_the_record_round_trip_through_vecten_toml(tmp_path):
    (tmp_path / "vecten.toml").write_text(
        'users = 1\n[project]\nname = "acme"\nscaffold_version = "0.1.0"\n'
        '[warehouse]\nkind = "snowflake"\naccount = "ACME-PROD"\nshared_account = true\n'
    )
    config = load_config(tmp_path)
    assert config.warehouse.shared_account is True
    config.environments["prod"].snowflake_shared_account = False
    save_config(tmp_path, config)
    written = tomllib.loads((tmp_path / "vecten.toml").read_text())
    assert written["warehouse"]["shared_account"] is True
    assert written["environments"]["prod"] == {"snowflake_shared_account": False}
