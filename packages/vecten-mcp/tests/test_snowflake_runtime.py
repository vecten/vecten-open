"""The MCP's Snowflake connections take every name from `snowflake_names`."""

from functools import partial

from vecten_mcp import runtime_setup
from vecten_shared.config import VectenConfig
from vecten_shared.provider import snowflake_names


def _config():
    return VectenConfig.model_validate(
        {
            "users": 1,
            "components": ["platform", "mcp"],
            "project": {"name": "acme", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "account": "ACME-PROD"},
        }
    )


class Keys:
    def __init__(self):
        self.minted = []

    def snowflake_token(self, **kwargs):
        self.minted.append(kwargs)
        return "token"


def _adapter(monkeypatch, layout, env):
    logins = []
    monkeypatch.setattr(runtime_setup, "snowflake_names", layout)
    monkeypatch.setattr(runtime_setup, "provisioner_key", lambda secrets: "KEY")
    keys = Keys()
    adapter = runtime_setup.snowflake_adapter(
        _config(), env, secrets=None, keys=keys, connect=lambda **kw: logins.append(kw)
    )
    adapter.provisioner_connect()
    adapter.mint_token(subject="JANE_DOE", roles={"ANALYST"}, expires_at=1)
    return adapter, logins, keys


def test_the_mcp_signs_in_as_the_fixed_provisioner(monkeypatch):
    adapter, logins, keys = _adapter(monkeypatch, snowflake_names, "prod")
    assert logins == [
        {
            "account": "ACME-PROD",
            "user": "VECTEN_PROVISIONER",
            "role": "PROVISIONER",
            "private_key": "KEY",
            "database": "ACME",
            "warehouse": "ACME_PROD_WH",
        }
    ]
    assert keys.minted[0]["audience"] == "https://ACME-PROD.snowflakecomputing.com"
    assert (adapter.account, adapter.database, adapter.warehouse) == (
        "ACME-PROD",
        "ACME",
        "ACME_PROD_WH",
    )


def test_the_mcp_signs_in_as_the_names_provisioner(monkeypatch):
    shared = partial(snowflake_names, shared_account=True)
    adapter, logins, _ = _adapter(monkeypatch, shared, "dev")
    (login,) = logins
    assert (login["user"], login["role"]) == ("PROVISIONER_ACME_DEV", "PROVISIONER_ACME_DEV")
    assert (login["database"], login["warehouse"]) == ("ACME_DEV", "ACME_DEV_WH")
    assert adapter.names == shared(_config(), "dev")


def test_the_mcp_signs_in_to_the_account_ship_handed_its_host(monkeypatch):
    monkeypatch.setenv("VECTEN_SNOWFLAKE_ACCOUNT", "ACME-DEV")
    adapter, logins, keys = _adapter(monkeypatch, snowflake_names, "dev")
    assert logins[0]["account"] == adapter.account == "ACME-DEV"
    assert keys.minted[0]["audience"] == "https://ACME-DEV.snowflakecomputing.com"


def test_the_mcp_names_its_objects_by_the_layout_vecten_toml_records(monkeypatch):
    config = _config()
    config.environments["dev"].snowflake_shared_account = True
    monkeypatch.setattr(runtime_setup, "provisioner_key", lambda secrets: "KEY")
    adapter = runtime_setup.snowflake_adapter(config, "dev", secrets=None, keys=Keys())
    assert adapter.names.provisioner_role == "PROVISIONER_ACME_DEV"
