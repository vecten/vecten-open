import pytest

from vecten_platform.sync.context import RuntimeContext
from vecten_platform.sync.destination import build_destination, warehouse_destination
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec


def test_postgres_destination_carries_the_spec_and_password():
    spec = ConnectionSpec(
        kind="postgres",
        database="warehouse",
        role="loader",
        auth="password",
        host="localhost",
        port=55432,
    )
    dest = build_destination(spec, "pw")
    assert dest.destination_type.endswith("postgres")
    creds = dest.config_params["credentials"]
    assert creds == {
        "host": "localhost",
        "port": 55432,
        "username": "loader",
        "password": "pw",
        "database": "warehouse",
    }


def test_snowflake_destination_carries_account_role_and_warehouse():
    spec = ConnectionSpec(
        kind="snowflake",
        database="DW",
        role="loader",
        auth="password",
        account="xy12345.eu-central-1",
        warehouse="LOAD_WH",
    )
    dest = build_destination(spec, "pw")
    assert dest.destination_type.endswith("snowflake")
    assert dest.config_params["credentials"] == {
        "host": "xy12345.eu-central-1",
        "username": "loader",
        "password": "pw",
        "database": "DW",
        "role": "loader",
        "warehouse": "LOAD_WH",
    }


def test_non_password_auth_is_refused():
    spec = ConnectionSpec(
        kind="postgres", database="warehouse", role="loader", auth="impersonation", host="h"
    )
    with pytest.raises(Failure) as exc:
        build_destination(spec, "pw")
    assert exc.value.code == ExitCode.PREREQUISITE


def test_unknown_warehouse_kind_is_refused():
    spec = ConnectionSpec(kind="bigquery", database="dw", role="loader", auth="password")
    with pytest.raises(Failure) as exc:
        build_destination(spec, "pw")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "bigquery" in exc.value.message


PEM = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"


def test_snowflake_key_pair_destination_carries_the_private_key_and_no_password():
    spec = ConnectionSpec(
        kind="snowflake",
        database="ACME",
        role="LOADER",
        auth="key_pair",
        account="ACME-PROD",
        warehouse="ACME_PROD_WH",
    )
    dest = build_destination(spec, PEM)
    assert dest.config_params["credentials"] == {
        "host": "ACME-PROD",
        "username": "LOADER",
        "private_key": PEM,
        "database": "ACME",
        "role": "LOADER",
        "warehouse": "ACME_PROD_WH",
    }


def test_key_pair_on_postgres_is_refused():
    spec = ConnectionSpec(kind="postgres", database="w", role="loader", auth="key_pair", host="h")
    with pytest.raises(Failure) as exc:
        build_destination(spec, PEM)
    assert exc.value.code == ExitCode.PREREQUISITE


class Provider:
    """Answers one connection and records the secret names read."""

    def __init__(self, spec: ConnectionSpec, secret: str):
        self.spec = spec
        self.secret = secret
        self.read: list[str] = []

    def connection(self, env, name):
        return self.spec

    def get_secret(self, name):
        self.read.append(name)
        return self.secret


def _ctx(tmp_path, provider, env):
    return RuntimeContext(tmp_path, tmp_path / "connectors", env, provider, tmp_path / "dlt")


def test_a_deployed_snowflake_project_loads_as_the_loader_by_key_pair(tmp_path):
    analysts = ConnectionSpec(
        kind="snowflake",
        database="ACME",
        role="ANALYST",
        auth="external_oauth",
        account="ACME-PROD",
        warehouse="ACME_PROD_WH",
    )
    provider = Provider(analysts, PEM)
    dest = warehouse_destination(_ctx(tmp_path, provider, "prod"))
    assert provider.read == ["SNOWFLAKE_LOADER_PRIVATE_KEY"]
    creds = dest.config_params["credentials"]
    assert creds["username"] == "LOADER" and creds["role"] == "LOADER"
    assert creds["private_key"] == PEM and "password" not in creds


def test_a_postgres_project_loads_with_the_loader_password(tmp_path):
    spec = ConnectionSpec(
        kind="postgres",
        database="warehouse",
        role="loader",
        auth="password",
        host="postgres",
        port=5432,
    )
    provider = Provider(spec, "pw")
    dest = warehouse_destination(_ctx(tmp_path, provider, "local"))
    assert provider.read == ["LOADER_PASSWORD"]
    assert dest.config_params["credentials"]["password"] == "pw"
