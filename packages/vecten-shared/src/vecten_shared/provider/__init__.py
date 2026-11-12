"""The provider interface: the one seam between the product and a cloud."""

from __future__ import annotations

import os
import re
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
    """Every name Vecten gives an object in an environment's Snowflake account.

    `access_schema` sits inside `database`, and `authentication_policy` inside that schema;
    the fixed layout has no policy. `comment` is the one the Snowflake root gives every
    object it creates, unknown without the account.
    """

    account: str | None
    suffix: str
    database: str
    warehouse: str
    analyst_role: str
    loader_role: str
    loader_user: str
    provisioner_role: str
    provisioner_user: str
    integration: str
    access_schema: str
    authentication_policy: str | None
    comment: str | None

    @property
    def audience(self) -> str:
        """The audience the account's External OAuth integration accepts in a token."""
        if not self.account:
            raise ValueError("the Snowflake audience needs the account")
        return f"https://{self.account}.snowflakecomputing.com"


# The environment's Snowflake account, which `ship` writes into every host's Compose
# environment from the environment's tfvars.
SNOWFLAKE_ACCOUNT_VAR = "VECTEN_SNOWFLAKE_ACCOUNT"


def shipped_account() -> str | None:
    """The Snowflake account `ship` handed this host; None off a deployed host."""
    return os.environ.get(SNOWFLAKE_ACCOUNT_VAR) or None


ACCESS_SCHEMA = "VECTEN_ACCESS"
AUTHENTICATION_POLICY = "MCP_SIGN_IN"


# Every account-level name is at most 63 characters before the suffix, so with a suffix of
# at most 64 each fits Snowflake's 255-character limit.
SUFFIX_LIMIT = 64
_SUFFIX = re.compile(r"_[A-Z0-9_]+")


def _identifier(value: str) -> str:
    # A project or environment name may hold a hyphen, which an unquoted identifier cannot.
    return value.replace("-", "_").upper()


def recorded_layout(config: VectenConfig, env: str) -> bool | None:
    """The layout `env`'s first deploy recorded; fixed for an environment deployed before
    layouts were recorded, None for one never deployed."""
    facts = config.environments.get(env)
    if facts is None:
        return None
    if facts.snowflake_shared_account is not None:
        return facts.snowflake_shared_account
    return False if facts.deployed_commit else None


def shared_layout(config: VectenConfig, env: str) -> bool:
    """Whether `env`'s Snowflake names carry its suffix: its recorded layout, else the
    project's setting."""
    recorded = recorded_layout(config, env)
    return config.warehouse.shared_account if recorded is None else recorded


def shared_base(config: VectenConfig) -> str:
    """`BASE` of a shared account's names: `[warehouse] database`, else the project name."""
    return _identifier(config.warehouse.database or config.project.name)


def _shared_suffix(config: VectenConfig, env: str) -> str:
    if "-" in env:
        raise Failure(
            ExitCode.CONFIG,
            f"environment {env!r} holds a hyphen, which the names of a shared Snowflake "
            f"account cannot carry, since `_` separates the project from the environment in "
            f"them; name it {env.replace('-', '')!r}",
        )
    base = config.warehouse.database or config.project.name
    suffix = "_" + _identifier(f"{base}_{env}")
    if not _SUFFIX.fullmatch(suffix):
        raise Failure(
            ExitCode.CONFIG,
            f"[warehouse] database {base!r} gives the environment suffix {suffix!r}, which "
            "an unquoted Snowflake name cannot end in; use letters, digits, `_` and `-`",
        )
    if len(suffix) > SUFFIX_LIMIT:
        raise Failure(
            ExitCode.CONFIG,
            f"environment {env!r} gives the suffix {suffix} of {len(suffix)} characters; a "
            f"shared Snowflake account allows {SUFFIX_LIMIT}, so shorten [warehouse] database "
            "(or the project name when it is unset) or the environment's name",
        )
    return suffix


def snowflake_names(
    config: VectenConfig,
    env: str,
    *,
    account: str | None = None,
    shared_account: bool | None = None,
) -> SnowflakeNames:
    """The names for `env` in `account`, by default the one `[warehouse] account` names.

    The layout is the environment's own (`shared_layout`) unless given. The
    fixed layout is the one every environment deployed before shared accounts carries, so
    each of its names must stay exactly as it is; a shared account ends every account-level
    name in the environment's suffix `_<BASE>_<ENV>`."""
    if shared_account is None:
        shared_account = shared_layout(config, env)
    account = account or config.warehouse.account
    # The Snowflake root's `local.comment`, from the same tfvars values.
    comment = f"vecten {config.project.name} {env} in {account}" if account else None
    project = config.project.name.replace("-", "_")
    if not shared_account:
        loader = "LOADER"
        return SnowflakeNames(
            account=account,
            suffix="",
            database=(config.warehouse.database or project).upper(),
            warehouse=_identifier(f"{project}_{env}_WH"),
            analyst_role="ANALYST",
            loader_role=loader,
            loader_user=loader,
            provisioner_role="PROVISIONER",
            provisioner_user="VECTEN_PROVISIONER",
            integration="VECTEN_EXTERNAL_OAUTH",
            access_schema=ACCESS_SCHEMA,
            authentication_policy=None,
            comment=comment,
        )
    suffix = _shared_suffix(config, env)
    base = suffix[1:]
    # dlt and dbt pass one name as both the loader's user and its role.
    loader = f"LOADER{suffix}"
    return SnowflakeNames(
        account=account,
        suffix=suffix,
        database=base,
        warehouse=f"{base}_WH",
        analyst_role=f"ANALYST{suffix}",
        loader_role=loader,
        loader_user=loader,
        provisioner_role=f"PROVISIONER{suffix}",
        provisioner_user=f"PROVISIONER{suffix}",
        integration=f"EXTERNAL_OAUTH{suffix}",
        access_schema=ACCESS_SCHEMA,
        authentication_policy=AUTHENTICATION_POLICY,
        comment=comment,
    )
