import pytest

from vecten_platform.dbt.profile import profile_env
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec


def test_postgres_profile_env():
    spec = ConnectionSpec(
        kind="postgres",
        database="warehouse",
        role="loader",
        auth="password",
        host="127.0.0.1",
        port=55432,
    )
    env = profile_env(spec, "s3cret", threads=2)
    assert env == {
        "WAREHOUSE_HOST": "127.0.0.1",
        "WAREHOUSE_PORT": "55432",
        "WAREHOUSE_USER": "loader",
        "WAREHOUSE_PASSWORD": "s3cret",
        "WAREHOUSE_DATABASE": "warehouse",
        "DBT_TARGET": "local",
        "DBT_THREADS": "2",
    }


def test_snowflake_profile_env():
    spec = ConnectionSpec(
        kind="snowflake",
        database="ANALYTICS",
        role="LOADER",
        auth="password",
        account="ab12345.eu-central-1",
        warehouse="LOAD_WH",
    )
    env = profile_env(spec, "pw")
    assert env["SNOWFLAKE_ACCOUNT"] == "ab12345.eu-central-1" and env["SNOWFLAKE_USER"] == "LOADER"
    assert env["SNOWFLAKE_ROLE"] == "LOADER" and env["SNOWFLAKE_WAREHOUSE"] == "LOAD_WH"
    assert env["SNOWFLAKE_DATABASE"] == "ANALYTICS" and env["DBT_THREADS"] == "4"


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (ConnectionSpec(kind="postgres", database="w", role="r", auth="password", port=1), "host"),
        (
            ConnectionSpec(kind="postgres", database="w", role="r", auth="password", host="h"),
            "port",
        ),
        (ConnectionSpec(kind="snowflake", database="w", role="r", auth="password"), "account"),
    ],
)
def test_profile_env_names_the_missing_field(spec, field):
    with pytest.raises(Failure) as exc:
        profile_env(spec, "pw")
    assert exc.value.code == ExitCode.PREREQUISITE and field in exc.value.message


def test_snowflake_key_pair_profile_env_carries_the_key_and_no_password():
    spec = ConnectionSpec(
        kind="snowflake",
        database="ACME",
        role="LOADER",
        auth="key_pair",
        account="ACME-PROD",
        warehouse="ACME_PROD_WH",
    )
    pem = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"
    env = profile_env(spec, pem)
    assert env["SNOWFLAKE_PRIVATE_KEY"] == pem
    assert "SNOWFLAKE_PASSWORD" not in env
    assert env["SNOWFLAKE_USER"] == "LOADER" and env["SNOWFLAKE_ROLE"] == "LOADER"
    assert env["SNOWFLAKE_ACCOUNT"] == "ACME-PROD"


def test_snowflake_password_profile_env_carries_no_key():
    spec = ConnectionSpec(
        kind="snowflake", database="A", role="LOADER", auth="password", account="x"
    )
    env = profile_env(spec, "pw")
    assert env["SNOWFLAKE_PASSWORD"] == "pw"
    assert "SNOWFLAKE_PRIVATE_KEY" not in env
