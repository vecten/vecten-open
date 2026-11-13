"""The dlt destination, derived from the provider's connection and the loader's credential."""

from __future__ import annotations

from dataclasses import replace

import dlt

from vecten_platform.sync.context import RuntimeContext
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec, snowflake_names

LOADER_SECRET = "LOADER_PASSWORD"
SNOWFLAKE_LOADER_KEY_SECRET = "SNOWFLAKE_LOADER_PRIVATE_KEY"


def build_destination(spec: ConnectionSpec, secret: str):
    """`secret` is the password, or the PEM private key when `spec.auth` is `key_pair`."""
    key_pair = spec.kind == "snowflake" and spec.auth == "key_pair"
    if spec.auth != "password" and not key_pair:
        raise Failure(
            ExitCode.PREREQUISITE,
            "the loader connects with a password, or by key pair on Snowflake; "
            f"the provider offered {spec.auth!r} on {spec.kind}",
        )
    if spec.kind == "postgres":
        return dlt.destinations.postgres(
            credentials={
                "host": spec.host,
                "port": spec.port,
                "username": spec.role,
                "password": secret,
                "database": spec.database,
            }
        )
    if spec.kind == "snowflake":
        credential = {"private_key": secret} if key_pair else {"password": secret}
        return dlt.destinations.snowflake(
            credentials={
                "host": spec.account,
                "username": spec.role,
                **credential,
                "database": spec.database,
                "role": spec.role,
                "warehouse": spec.warehouse,
            }
        )
    raise Failure(
        ExitCode.PREREQUISITE,
        f"no loader destination for warehouse kind {spec.kind!r}; one of: postgres, snowflake",
    )


def loader_connection(ctx: RuntimeContext) -> tuple[ConnectionSpec, str]:
    """The connection dlt and dbt load and build through, and its credential.

    On Snowflake the provider's warehouse connection is the analysts'; loads and builds run
    as the loader service user, which signs in only by key pair. `build_destination` passes
    the role as the user, which holds because the loader's user and role share one name.
    """
    spec = ctx.provider.connection(ctx.env, "warehouse")
    if spec.kind == "snowflake":
        names = snowflake_names(load_config(ctx.project_root), ctx.env)
        spec = replace(spec, role=names.loader_role, auth="key_pair")
        return spec, ctx.provider.get_secret(SNOWFLAKE_LOADER_KEY_SECRET)
    return spec, ctx.provider.get_secret(LOADER_SECRET)


def warehouse_destination(ctx: RuntimeContext):
    return build_destination(*loader_connection(ctx))
