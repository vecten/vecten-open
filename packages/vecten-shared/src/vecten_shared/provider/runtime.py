"""What every cloud's runtime provider shares: connections and logs from the outputs file.

A cloud subclass adds its secret store. The control-plane operations are refused here
because code on the VMs never holds the credentials they need.
"""

from __future__ import annotations

from pathlib import Path

from vecten_shared.config import VectenConfig, load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import (
    ROLE_FOR_DB,
    ConnectionSpec,
    DbName,
    LogTarget,
    check_db_name,
    shipped_account,
    snowflake_names,
)
from vecten_shared.provider.environment import EnvironmentOutputs, environment_file, load_outputs

# Only standing MCP credentials may be rotated by the runtime.
RUNTIME_WRITABLE = frozenset({"MCP_IMPERSONATOR_PASSWORD", "MCP_SIGNING_KEY"})
EXISTING_WAREHOUSE_ROLE = "impersonator"
POSTGRES_PORT = 5432


class OutputsRuntimeProvider:
    def __init__(self, project_root: Path, env: str):
        self.project_root = project_root
        self.env = env
        self._outputs: EnvironmentOutputs | None = None
        self._config: VectenConfig | None = None

    def outputs(self) -> EnvironmentOutputs:
        if self._outputs is None:
            path = environment_file(self.project_root, self.env)
            outputs = load_outputs(path, self.env)
            if outputs.environment != self.env:
                raise Failure(
                    ExitCode.CONFIG,
                    f"{path} holds the outputs of environment {outputs.environment!r}, "
                    f"not {self.env!r}",
                )
            self._outputs = outputs
        return self._outputs

    def config(self) -> VectenConfig:
        if self._config is None:
            self._config = load_config(self.project_root)
        return self._config

    def region(self) -> str:
        return self.outputs().region

    def _check_env(self, env: str) -> None:
        if env != self.env:
            raise Failure(
                ExitCode.USAGE, f"this provider is bound to environment {self.env!r}, not {env!r}"
            )

    def _cli_only(self, operation: str) -> Failure:
        return Failure(
            ExitCode.REFUSED,
            f"{operation} is reachable only from the vecten CLI, "
            f"not from code running in environment {self.env!r}",
        )

    def _write_refused(self, name: str) -> Failure:
        return Failure(
            ExitCode.REFUSED,
            f"writing secret {name} is reachable only from the vecten CLI; code running "
            f"in an environment writes only {', '.join(sorted(RUNTIME_WRITABLE))}",
        )

    def bootstrap(self, env: str, repository: str) -> None:
        raise self._cli_only("bootstrap")

    def provision(self, env: str) -> dict:
        raise self._cli_only("provision")

    def destroy(self, env: str) -> None:
        raise self._cli_only("destroy")

    def ship(self, env: str, component: str, project_dir: Path) -> None:
        raise self._cli_only("ship")

    def may_write(self, name: str) -> bool:
        return name in RUNTIME_WRITABLE

    def connection(self, env: str, db: DbName) -> ConnectionSpec:
        check_db_name(db)
        self._check_env(env)
        if db == "warehouse":
            warehouse = self.config().warehouse
            if warehouse.kind == "snowflake":
                return self._snowflake_warehouse()
            if warehouse.existing:
                return self._existing_postgres_warehouse()
        for instance in self.outputs().postgres.values():
            if db in instance.databases:
                return ConnectionSpec(
                    kind="postgres",
                    database=db,
                    role=ROLE_FOR_DB[db],
                    auth="password",
                    host=instance.host,
                    port=instance.port,
                )
        raise Failure(
            ExitCode.PREREQUISITE,
            f"environment {self.env!r} has no {db} database in its outputs; "
            f"run `vecten deploy --env {self.env}`",
        )

    def _existing_postgres_warehouse(self) -> ConnectionSpec:
        # No warehouse instance exists in the outputs here, and the impersonator is the
        # only login the MCP server holds on a warehouse it did not create.
        warehouse = self.config().warehouse
        for key in ("host", "database"):
            if getattr(warehouse, key) is None:
                raise Failure(
                    ExitCode.CONFIG,
                    f"vecten.toml: [warehouse] {key} is required for an existing Postgres "
                    "warehouse",
                )
        return ConnectionSpec(
            kind="postgres",
            database=warehouse.database,
            role=EXISTING_WAREHOUSE_ROLE,
            auth="impersonation",
            host=warehouse.host,
            port=warehouse.port or POSTGRES_PORT,
        )

    def _snowflake_warehouse(self) -> ConnectionSpec:
        config = self.config()
        names = snowflake_names(config, self.env, account=shipped_account())
        if not names.account:
            raise Failure(
                ExitCode.CONFIG, "vecten.toml: warehouse.account is required on Snowflake"
            )
        return ConnectionSpec(
            kind="snowflake",
            database=names.database,
            role=names.analyst_role,
            auth="external_oauth",
            account=names.account,
            warehouse=names.warehouse,
        )

    def log_target(self, env: str) -> LogTarget:
        self._check_env(env)
        return LogTarget(kind="bucket", location=f"s3://{self.outputs().bucket}/platform/logs")
