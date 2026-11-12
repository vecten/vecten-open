"""vecten.toml: the one editable configuration file of a project."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

import tomli_w
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from vecten_shared.names import (
    connector_name_problem,
    env_name_problem,
    group_name_problem,
    secret_name_problem,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic
from vecten_shared.requirements import REQUIREMENT_RULE, parse_requirement

CONFIG_FILE = "vecten.toml"
Component = Literal["platform", "mcp"]
Size = Literal["small", "medium", "large"]
Profile = Literal["standard", "minimal"]
WarehouseKind = Literal["postgres", "snowflake"]
Cloud = Literal["aws", "scaleway"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectSection(Strict):
    name: str
    scaffold_version: str
    convention_version: int = 1


class WarehouseSection(Strict):
    kind: WarehouseKind = "postgres"
    existing: bool = False
    host: str | None = None
    port: int | None = None
    database: str | None = None
    account: str | None = None
    # The login introspection signs in as, named by warehouse_url.
    admin_user: str | None = None


_REGION = re.compile(r"[a-z0-9-]{2,32}")
_HOSTNAME_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
# The Snowflake provider and the root's variable take the account as its organisation and
# account names; an account locator such as `xy12345.eu-central-1` names neither.
SNOWFLAKE_ACCOUNT = re.compile(r"([A-Za-z0-9]+)-([A-Za-z0-9_]+)")
HOSTNAME_RULE = (
    "a lowercase hostname: dot-separated labels of 1 to 63 letters, digits or inner hyphens, "
    "at least two labels, at most 253 characters"
)
DEPLOY_VALUE_RULES = {
    "region": "lowercase letters, digits and hyphens, 2 to 32 characters",
    "domain": HOSTNAME_RULE,
    "dns_zone": HOSTNAME_RULE,
    "github_repository": "owner/name on GitHub, the owner not starting with a hyphen",
    "snowflake_account": "an ORGANIZATION-ACCOUNT identifier, for example ACME-PROD",
}


def _is_hostname(value: str) -> bool:
    labels = value.split(".")
    return (
        len(value) <= 253 and len(labels) >= 2 and all(_HOSTNAME_LABEL.fullmatch(x) for x in labels)
    )


def deploy_value_problem(key: str, value: str) -> str | None:
    """The refusal for a deploy answer, or None when it is valid.

    These values land unquoted in Terraform, workflow YAML and NGINX server names, so
    anything outside the rule could change the meaning of a generated file.
    """
    if key == "region":
        ok = bool(_REGION.fullmatch(value))
    elif key in ("domain", "dns_zone"):
        ok = _is_hostname(value)
    elif key == "github_repository":
        ok = bool(_GITHUB_REPOSITORY.fullmatch(value)) and not value.startswith("-")
    elif key == "snowflake_account":
        ok = bool(SNOWFLAKE_ACCOUNT.fullmatch(value))
    else:
        return None
    return None if ok else f"{key} must be {DEPLOY_VALUE_RULES[key]}; got {value!r}"


def check_deploy_value(key: str, value: str | None) -> str | None:
    problem = None if value is None else deploy_value_problem(key, value)
    if problem:
        raise ValueError(problem)
    return value


class DeploySection(Strict):
    cloud: Cloud
    region: str
    domain: str
    dns_zone: str
    github_repository: str

    @field_validator("region", "domain", "dns_zone", "github_repository")
    @classmethod
    def _deploy_value(cls, value: str, info: ValidationInfo) -> str:
        return check_deploy_value(info.field_name, value)


McpAuth = Literal["oidc", "password"]


def allow_networks(value: list[str]) -> list[str]:
    # Each entry becomes an NGINX `allow` line, so only a parsed network may pass.
    return [str(ipaddress.ip_network(item, strict=True)) for item in value]


class McpSection(Strict):
    admins: list[str] = Field(default_factory=list)
    auth: str = Field(default="oidc", pattern=r"^[a-z][a-z0-9_-]*$")
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    admin_panel: Literal["public", "off"] = "public"
    admin_allow: list[str] = Field(default_factory=list)

    @field_validator("admin_allow")
    @classmethod
    def _networks(cls, value: list[str]) -> list[str]:
        return allow_networks(value)

    @model_validator(mode="after")
    def _exposure(self) -> McpSection:
        if self.admin_panel == "off" and self.admin_allow:
            raise ValueError("admin_allow applies only while admin_panel is public")
        return self


class AirflowSection(Strict):
    # The networks that may reach a deployed `airflow.<domain>`; empty leaves it public.
    allow: list[str] = Field(default_factory=list)

    @field_validator("allow")
    @classmethod
    def _networks(cls, value: list[str]) -> list[str]:
        return allow_networks(value)


def _checked(problem: str | None, value: Any) -> Any:
    if problem:
        raise ValueError(problem)
    return value


class AccessDecl(Strict):
    # Required and non-empty: an access answer with no group would grant nothing yet look done.
    groups: list[str] = Field(min_length=1)

    @field_validator("groups")
    @classmethod
    def _groups(cls, value: list[str]) -> list[str]:
        for group in value:
            _checked(group_name_problem(group), group)
        return value


SourceKind = Literal["dlt", "rest", "sql", "filesystem", "custom", "vecten-managed"]
SOURCE_KINDS: tuple[str, ...] = ("dlt", "rest", "sql", "filesystem", "custom", "vecten-managed")
GENERIC_KINDS: tuple[str, ...] = ("rest", "sql", "filesystem")


class ConnectorDecl(Strict):
    name: str
    source: SourceKind
    access: AccessDecl | None = None
    dependencies: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)
    # An empty object list means every resource the source yields, discovered at run time.
    groups: dict[str, list[str]] = Field(default_factory=lambda: {"default": []})
    extension: str | None = None
    version: str | None = None
    scaffold_hash: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _checked(connector_name_problem(value), value)

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: list[str]) -> list[str]:
        for secret in value:
            _checked(secret_name_problem(secret), secret)
        return value

    @field_validator("groups")
    @classmethod
    def _dag_groups(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        # The group names the DAG file `sync_<name>_<group>.py`.
        for group in value:
            _checked(group_name_problem(group), group)
        return value


# Cloud identifiers land in tfvars, workflow variables and API paths.
_CLOUD_ID_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
_CLOUD_ID_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# `cloud_ids` keys with this prefix hold when a key bootstrap made expires, not an identifier.
CLOUD_ID_EXPIRY_PREFIX = "expires_"
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _is_utc_timestamp(value: str) -> bool:
    if not _UTC_TIMESTAMP.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class EnvironmentFacts(Strict):
    bootstrapped: bool = False
    state_bucket: str | None = None
    # Non-secret identifiers `bootstrap` creates, such as the Scaleway project IDs.
    cloud_ids: dict[str, str] = Field(default_factory=dict)
    deployed_commit: str | None = None
    first_user_added: bool = False
    connector_access_applied: list[str] = Field(default_factory=list)
    provisioner_script_run: bool = False
    provisioner_secrets_set: bool = False
    connection_shown: bool = False
    alert_webhook_asked: bool = False
    oidc_registered: bool = False
    admin_link_issued: bool = False

    @field_validator("cloud_ids")
    @classmethod
    def _cloud_ids(cls, value: dict[str, str]) -> dict[str, str]:
        for key, ident in value.items():
            if key.startswith(CLOUD_ID_EXPIRY_PREFIX):
                if not _CLOUD_ID_KEY.fullmatch(key) or not _is_utc_timestamp(ident):
                    raise ValueError(
                        f"cloud_ids {key!r} is a key's expiry, an ISO timestamp in UTC such "
                        "as 2027-10-18T12:00:00Z"
                    )
                continue
            if not _CLOUD_ID_KEY.fullmatch(key) or not _CLOUD_ID_VALUE.fullmatch(ident):
                raise ValueError(
                    "cloud_ids keys are lowercase identifiers and values plain identifiers "
                    f"of letters, digits, '.', '_' and '-'; got {key!r}"
                )
        return value


class _EnvDict(dict):
    def __missing__(self, key):
        self[key] = EnvironmentFacts()
        return self[key]


class PackagesSection(Strict):
    private_index: bool = False


class ComponentDependencies(Strict):
    python: list[str] = Field(default_factory=list)

    @field_validator("python")
    @classmethod
    def _python(cls, value: list[str]) -> list[str]:
        for entry in value:
            if parse_requirement(entry) is None:
                raise ValueError(f"{entry!r}: {REQUIREMENT_RULE}")
        return value


class DependenciesSection(Strict):
    platform: ComponentDependencies | None = None
    mcp: ComponentDependencies | None = None
    cli: ComponentDependencies | None = None

    def python(self, component: str) -> list[str]:
        section = getattr(self, component)
        return list(section.python) if section is not None else []


class VectenConfig(Strict):
    packages: PackagesSection = Field(default_factory=PackagesSection)
    users: int = Field(gt=0)
    size: Size = "small"
    components: list[Component] = Field(default_factory=lambda: ["platform", "mcp"])
    profile: Profile = "standard"
    wanted: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    project: ProjectSection
    warehouse: WarehouseSection = Field(default_factory=WarehouseSection)
    deploy: DeploySection | None = None
    mcp: McpSection = Field(default_factory=McpSection)
    airflow: AirflowSection = Field(default_factory=AirflowSection)
    connectors: list[ConnectorDecl] = Field(default_factory=list)
    dependencies: DependenciesSection = Field(default_factory=DependenciesSection)
    environments: dict[str, EnvironmentFacts] = Field(default_factory=dict)

    @field_validator("environments")
    @classmethod
    def _environment_names(cls, value: dict[str, EnvironmentFacts]) -> dict[str, EnvironmentFacts]:
        for env in value:
            _checked(env_name_problem(env), env)
        return value

    @model_validator(mode="after")
    def _airflow_needs_platform(self) -> VectenConfig:
        # Without the platform no host serves Airflow, so the list would restrict nothing.
        if self.airflow.allow and not self.has("platform"):
            raise ValueError("[airflow] allow needs the platform component")
        return self

    @model_validator(mode="after")
    def _dependencies_need_their_component(self) -> VectenConfig:
        # No image would install them, so the requirements would be silently ignored.
        for component in ("platform", "mcp"):
            if getattr(self.dependencies, component) is not None and not self.has(component):
                raise ValueError(
                    f"[dependencies.{component}] needs the {component} component, "
                    "which this project does not enable"
                )
        return self

    def model_post_init(self, context: Any, /) -> None:
        self.environments = _EnvDict(self.environments)

    def has(self, component: Component) -> bool:
        return component in self.components

    def connector(self, name: str) -> ConnectorDecl | None:
        return next((c for c in self.connectors if c.name == name), None)


def load_config(project_root: Path) -> VectenConfig:
    path = project_root / CONFIG_FILE
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Failure(ExitCode.REFUSED, f"{path} not found; run `vecten init` first") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise Failure(ExitCode.CONFIG, f"{path}: {exc}") from None
    try:
        return VectenConfig.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise Failure(ExitCode.CONFIG, f"{path}: {loc}: {first['msg']}") from None


def stack_project_name(project_root: Path, project_name: str) -> str:
    # Compose scopes volumes by project name, so two checkouts sharing a project name
    # must still differ here or one would boot on the other's Postgres data.
    digest = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"vecten-{project_name}-{digest}"


def save_config(project_root: Path, config: VectenConfig) -> None:
    data = config.model_dump(mode="json", exclude_none=True)
    if not data.get("dependencies"):
        data.pop("dependencies", None)
    write_text_atomic(project_root / CONFIG_FILE, tomli_w.dumps(data))


def parse_warehouse_locator(url: str) -> WarehouseSection:
    parts = urlsplit(url)
    if parts.password or "password" in {k.lower() for k in parse_qs(parts.query)}:
        raise Failure(
            ExitCode.USAGE,
            "warehouse_url must not carry a password; store a Postgres password with "
            "`vecten secret set WAREHOUSE_ADMIN_PASSWORD`, while Snowflake introspection signs "
            "in with a key pair `vecten mcp introspect` generates",
        )
    scheme = parts.scheme.lower()
    admin_user = unquote(parts.username) if parts.username else None
    if scheme in ("postgres", "postgresql"):
        if not parts.hostname:
            raise Failure(ExitCode.USAGE, f"warehouse_url {url!r} needs a host")
        try:
            port = parts.port
        except ValueError as exc:
            message = f"warehouse_url {url!r} has an invalid port: {exc}"
            raise Failure(ExitCode.USAGE, message) from None
        return WarehouseSection(
            kind="postgres",
            existing=True,
            host=parts.hostname,
            port=port or 5432,
            database=parts.path.lstrip("/") or None,
            admin_user=admin_user,
        )
    if scheme == "snowflake":
        if not parts.hostname:
            raise Failure(ExitCode.USAGE, f"warehouse_url {url!r} needs an account")
        segments = [s for s in parts.path.split("/") if s]
        return WarehouseSection(
            kind="snowflake",
            existing=True,
            account=parts.hostname,
            database=segments[0] if segments else None,
            admin_user=admin_user,
        )
    raise Failure(
        ExitCode.USAGE,
        f"unsupported warehouse_url scheme {scheme!r}; use postgres://, postgresql:// or "
        "snowflake://, without a driver suffix such as +psycopg2",
    )
