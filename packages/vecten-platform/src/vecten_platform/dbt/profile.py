"""The environment dbt's generated `profiles.yml` reads its connection from."""

from __future__ import annotations

from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec


def _required(spec: ConnectionSpec, field: str) -> str:
    value = getattr(spec, field)
    if value is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"the {spec.kind} connection has no {field}; dbt cannot reach the warehouse",
        )
    return str(value)


def profile_env(
    spec: ConnectionSpec, secret: str, *, threads: int = 4, target: str = "local"
) -> dict[str, str]:
    """`secret` is the password, or the PEM private key when `spec.auth` is `key_pair`."""
    if spec.kind == "postgres":
        env = {
            "WAREHOUSE_HOST": _required(spec, "host"),
            "WAREHOUSE_PORT": _required(spec, "port"),
            "WAREHOUSE_USER": spec.role,
            "WAREHOUSE_PASSWORD": secret,
            "WAREHOUSE_DATABASE": spec.database,
        }
    else:
        # dbt-snowflake signs in with a non-empty private key over any password, so each spec
        # sets exactly the one credential its auth names.
        credential = (
            {"SNOWFLAKE_PRIVATE_KEY": secret}
            if spec.auth == "key_pair"
            else {"SNOWFLAKE_PASSWORD": secret}
        )
        env = {
            "SNOWFLAKE_ACCOUNT": _required(spec, "account"),
            "SNOWFLAKE_USER": spec.role,
            **credential,
            "SNOWFLAKE_ROLE": spec.role,
            "SNOWFLAKE_WAREHOUSE": spec.warehouse or "",
            "SNOWFLAKE_DATABASE": spec.database,
        }
    return {**env, "DBT_TARGET": target, "DBT_THREADS": str(threads)}
