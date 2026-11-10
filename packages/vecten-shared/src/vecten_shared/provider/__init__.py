"""The provider interface: the one seam between the product and a cloud."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from vecten_shared.names import check_secret_name
from vecten_shared.output import ExitCode, Failure

if TYPE_CHECKING:
    from vecten_shared.config import VectenConfig

DbName = Literal["warehouse", "airflow", "mcp_state"]
DB_NAMES: tuple[str, ...] = ("warehouse", "airflow", "mcp_state")
ROLE_FOR_DB = {"warehouse": "loader", "airflow": "airflow", "mcp_state": "mcp_state"}
PROVIDER_NAMES: tuple[str, ...] = ("aws", "scaleway", "local")

Scope = Literal["platform", "mcp", "deploy", "admin", "setup"]
SCOPES: tuple[str, ...] = ("platform", "mcp", "deploy", "admin", "setup")
# Cloud identities are granted secrets by scope, so the prefix of a name decides which
# machines can ever read its value. `setup` holds the database admin passwords only the
# setup host reads.
_SCOPE_PREFIXES: tuple[tuple[str, Scope], ...] = (
    ("MCP_", "mcp"),
    ("DEPLOY_", "deploy"),
    ("ADMIN_", "admin"),
    ("SETUP_", "setup"),
)


def scope_for(name: str) -> Scope:
    for prefix, scope in _SCOPE_PREFIXES:
        if name.startswith(prefix):
            return scope
    return "platform"


def secret_prefix(project: str, env: str) -> str:
    return f"vecten/{project}/{env}/"


def secret_id(project: str, env: str, name: str) -> str:
    check_secret_name(name)
    return f"{secret_prefix(project, env)}{scope_for(name)}/{name}"


@dataclass(frozen=True)
class ConnectionSpec:
    kind: Literal["postgres", "snowflake"]
    database: str
    role: str
    auth: Literal["password", "impersonation", "external_oauth", "key_pair"]
    host: str | None = None
    port: int | None = None
    account: str | None = None
    warehouse: str | None = None


@dataclass(frozen=True)
class LogTarget:
    kind: Literal["docker_volume", "bucket"]
    location: str


class Provider(Protocol):
    def bootstrap(self, env: str, repository: str) -> None: ...
    def provision(self, env: str) -> dict: ...
    def destroy(self, env: str) -> None: ...
    def put_secret(self, name: str, value: str) -> None: ...
    def get_secret(self, name: str) -> str: ...
    def list_secrets(self) -> list[str]: ...
    def connection(self, env: str, db: DbName) -> ConnectionSpec: ...
    def log_target(self, env: str) -> LogTarget: ...
    def ship(self, env: str, component: str, project_dir: Path) -> None: ...


ProviderFactory = Callable[[Path, str], Provider]
_REGISTRY: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    _REGISTRY[name] = factory


def provider_factory(name: str) -> ProviderFactory | None:
    return _REGISTRY.get(name)


def get_provider(name: str, project_root: Path, env: str = "local") -> Provider:
    if name not in PROVIDER_NAMES:
        raise Failure(
            ExitCode.USAGE, f"unknown provider {name!r}; one of: {', '.join(PROVIDER_NAMES)}"
        )
    from vecten_shared.provider import local  # noqa: F401  registers itself on import

    # The control-plane providers in `vecten` register themselves under the same names;
    # a runtime class fills its slot only when nothing holds it.
    if name == "aws" and name not in _REGISTRY:
        from vecten_shared.provider.aws import AwsRuntimeProvider

        register_provider("aws", AwsRuntimeProvider)
    if name == "scaleway" and name not in _REGISTRY:
        from vecten_shared.provider.scaleway import ScalewayRuntimeProvider

        register_provider("scaleway", ScalewayRuntimeProvider)
    factory = _REGISTRY.get(name)
    if factory is None:
        raise Failure(ExitCode.PREREQUISITE, f"provider {name!r} is not installed in this release")
    return factory(project_root, env)


def check_db_name(db: str) -> None:
    if db not in DB_NAMES:
        raise Failure(ExitCode.USAGE, f"unknown database {db!r}; one of: {', '.join(DB_NAMES)}")


@dataclass(frozen=True)
class SnowflakeNames:
    database: str
    warehouse: str


def snowflake_names(config: VectenConfig, env: str) -> SnowflakeNames:
    # A project or environment name may hold a hyphen, which an unquoted identifier cannot.
    project = config.project.name.replace("-", "_")
    database = (config.warehouse.database or project).upper()
    warehouse = f"{project}_{env}_WH".upper().replace("-", "_")
    return SnowflakeNames(database=database, warehouse=warehouse)
