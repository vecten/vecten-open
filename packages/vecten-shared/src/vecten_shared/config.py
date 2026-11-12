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
from packaging.version import InvalidVersion, Version
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from vecten_shared.declarations import PROJECT as PROJECT_SETTINGS
from vecten_shared.declarations import is_toml_time, secret_problem
from vecten_shared.names import (
    connector_name_problem,
    env_name_problem,
    group_name_problem,
    secret_name_problem,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic
from vecten_shared.requirements import REQUIREMENT_RULE, parse_requirement
from vecten_shared.services import CPUS, MEMORY_MB
from vecten_shared.services import name_problem as service_name_problem
from vecten_shared.versions import normalize_name

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


PROD_ENV = "prod"
DEFAULT_PROMOTION: tuple[str, ...] = (PROD_ENV,)
# The first release whose rendered workflows read `[deploy] promotion`; workflows rendered
# earlier ship prod on every push whatever the order says.
PROMOTION_SINCE = "0.1.1"


def promotion_problem(order: list[str]) -> str | None:
    """Why `order` is no valid `[deploy] promotion`, or None."""
    for env in order:
        problem = env_name_problem(env)
        if problem:
            return problem
    if len(order) < 2:
        return (
            f"lists at least two environments ending with {PROD_ENV}; leave it out to ship "
            f"{PROD_ENV} on every push"
        )
    repeated = sorted({env for env in order if order.count(env) > 1})
    if repeated:
        return f"names {', '.join(repeated)} more than once"
    if order[-1] != PROD_ENV or order.count(PROD_ENV) != 1:
        return f"must end with {PROD_ENV}, so that no environment promotes from it; got {order}"
    return None


class DeploySection(Strict):
    cloud: Cloud
    region: str
    domain: str
    dns_zone: str
    github_repository: str
    # The order code reaches environments in: a push ships the first, each later one ships
    # only what the one before it shipped. None means prod alone.
    promotion: list[str] | None = None

    @field_validator("region", "domain", "dns_zone", "github_repository")
    @classmethod
    def _deploy_value(cls, value: str, info: ValidationInfo) -> str:
        return check_deploy_value(info.field_name, value)

    @field_validator("promotion")
    @classmethod
    def _promotion(cls, value: list[str] | None) -> list[str] | None:
        return value if value is None else _checked(promotion_problem(value), value)


def promotion_scaffold_problem(config: VectenConfig) -> str | None:
    """Why the project may not carry a promotion order yet, or None.

    The workflows pin the release they were rendered with, and one older than
    `PROMOTION_SINCE` ships prod on every push whatever the order says.
    """
    if config.deploy is None or config.deploy.promotion is None:
        return None
    rendered = config.project.scaffold_version
    try:
        current = Version(rendered) >= Version(PROMOTION_SINCE)
    except InvalidVersion:
        current = False
    if current:
        return None
    return (
        f"[deploy] promotion needs workflows rendered by vecten {PROMOTION_SINCE} or later, "
        f"and this project's were rendered by {rendered}; run `vecten update` first"
    )


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


APEX = "@"
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# The stack's own hostnames, and `_`-prefixed names that DNS and ACME use for their records.
RESERVED_LABELS = frozenset({"airflow", "mcp", "www"})


def label_problem(value: str) -> str | None:
    """Why `value` cannot name a service's hostname below the domain, or None."""
    if value == APEX:
        return None
    if value in RESERVED_LABELS or value.startswith("_"):
        return f"{value} is reserved; choose another label"
    if not _LABEL.fullmatch(value):
        return "a DNS label of lowercase letters, digits and inner hyphens, or @ for the apex"
    return None


def service_hostname(label: str, domain: str) -> str:
    return domain if label == APEX else f"{label}.{domain}"


class RouteBinding(Strict):
    access: Literal["public", "login"]
    groups: list[str] = Field(default_factory=list)
    # Networks that may reach the route on a deployed host; empty admits every address.
    allow: list[str] = Field(default_factory=list)

    @field_validator("groups")
    @classmethod
    def _groups(cls, value: list[str]) -> list[str]:
        for group in value:
            _checked(group_name_problem(group), group)
        return value

    @field_validator("allow")
    @classmethod
    def _networks(cls, value: list[str]) -> list[str]:
        return allow_networks(value)

    @model_validator(mode="after")
    def _groups_match_access(self) -> RouteBinding:
        if self.access == "login" and not self.groups:
            raise ValueError("groups: a login route admits one or more groups")
        if self.access == "public" and self.groups:
            raise ValueError("groups: only a login route names groups")
        return self


class ServiceBinding(Strict):
    # A DNS label below the environment's domain, or `@` for the domain itself.
    hostname: str | None = None
    host: Component | None = None
    local: bool = True
    memory_mb: int | None = Field(default=None, ge=MEMORY_MB[0], le=MEMORY_MB[1])
    cpus: float | None = Field(default=None, ge=CPUS[0], le=CPUS[1])
    routes: dict[str, RouteBinding] = Field(default_factory=dict)

    @field_validator("hostname")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return value if value is None else _checked(label_problem(value), value)

    @field_validator("routes")
    @classmethod
    def _route_names(cls, value: dict[str, RouteBinding]) -> dict[str, RouteBinding]:
        for name in value:
            _checked(service_name_problem("route", name), name)
        return value

    @model_validator(mode="after")
    def _routes_need_a_hostname(self) -> ServiceBinding:
        if self.routes and self.hostname is None:
            raise ValueError("hostname is required when a route is bound")
        return self


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


# A Debian package name, optionally `=<version>`. Entries reach an `apt-get install` line of a
# generated Dockerfile, so nothing that a shell or apt would read as more than a name passes:
# apt reads a trailing `-` as "remove this package", and a version starts with a digit after an
# optional numeric epoch.
_DEBIAN_PACKAGE = re.compile(
    r"[a-z0-9][a-z0-9+.-]*[a-z0-9+.](=([0-9]+:)?[0-9]([A-Za-z0-9.+~-]*[A-Za-z0-9.+~])?)?"
)


class _PythonDependencies(Strict):
    python: list[str] = Field(default_factory=list)

    @field_validator("python")
    @classmethod
    def _python(cls, value: list[str]) -> list[str]:
        for entry in value:
            if parse_requirement(entry) is None:
                raise ValueError(f"{entry!r}: {REQUIREMENT_RULE}")
        return value


class CliDependencies(_PythonDependencies):
    @model_validator(mode="before")
    @classmethod
    def _no_system(cls, value: Any) -> Any:
        if isinstance(value, dict) and "system" in value:
            raise ValueError(
                "system: Vecten does not install operating-system packages on the laptop; "
                "install them yourself, and list the image's in [dependencies.platform] or "
                "[dependencies.mcp]"
            )
        return value


class ComponentDependencies(_PythonDependencies):
    system: list[str] = Field(default_factory=list)

    @field_validator("system")
    @classmethod
    def _system(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not _DEBIAN_PACKAGE.fullmatch(entry):
                raise ValueError(
                    f"{entry!r} is not a Debian package name, optionally with =<version>"
                )
        return value


class DependenciesSection(Strict):
    platform: ComponentDependencies | None = None
    mcp: ComponentDependencies | None = None
    cli: CliDependencies | None = None

    def python(self, component: str) -> list[str]:
        section = getattr(self, component)
        return list(section.python) if section is not None else []

    def system(self, component: str) -> list[str]:
        section = getattr(self, component)
        return list(getattr(section, "system", [])) if section is not None else []


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
    # Secret names the project's own DAGs and MCP code read, delivered like an extension's.
    secrets: list[str] = Field(default_factory=list)
    # `[settings.<extension>]` and `[settings.project]`, with `env.<env>` overrides.
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # `[services.<extension>.<service>]`: where and how each declared service runs.
    services: dict[str, dict[str, ServiceBinding]] = Field(default_factory=dict)
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

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: list[str]) -> list[str]:
        for name in value:
            problem = secret_problem(name)
            if problem:
                raise ValueError(problem)
        return value

    @field_validator("extensions")
    @classmethod
    def _no_extension_named_project(cls, value: list[str]) -> list[str]:
        for registration in value:
            req = parse_requirement(registration)
            if req is not None and normalize_name(req.name) == PROJECT_SETTINGS:
                raise ValueError(
                    "an extension may not be named project, which names the project's settings"
                )
        return value

    @model_validator(mode="after")
    def _settings_namespaces(self) -> VectenConfig:
        listed = {
            normalize_name(req.name)
            for req in map(parse_requirement, self.extensions)
            if req is not None
        }
        for namespace, table in self.settings.items():
            where = f"settings.{namespace}"
            if namespace != PROJECT_SETTINGS and namespace not in listed:
                raise ValueError(f"{where}: no extension of that name is listed in extensions")
            if is_toml_time(table):
                raise ValueError(
                    f"{where}: a TOML date or time, inf or nan has no JSON type; use a string"
                )
            overrides = table.get("env", {})
            if not isinstance(overrides, dict):
                raise ValueError(f"{where}.env: a table of environments, such as env.prod")
            for env, values in overrides.items():
                if env != "local" and env_name_problem(env):
                    raise ValueError(f"{where}.env.{env}: {env_name_problem(env)}")
                if not isinstance(values, dict):
                    raise ValueError(f"{where}.env.{env}: a table of settings")
        return self

    @model_validator(mode="after")
    def _service_bindings(self) -> VectenConfig:
        listed = {
            normalize_name(req.name)
            for req in map(parse_requirement, self.extensions)
            if req is not None
        }
        labels: set[str] = set()
        for extension, bindings in self.services.items():
            if extension not in listed:
                raise ValueError(
                    f"services.{extension}: no extension of that name is listed in extensions"
                )
            for name, binding in bindings.items():
                where = f"services.{extension}.{name}"
                problem = service_name_problem("service", name)
                if problem:
                    raise ValueError(f"{where}: {problem}")
                if binding.host is not None and not self.has(binding.host):
                    raise ValueError(f"{where}.host: the project does not enable {binding.host}")
                label = binding.hostname
                if label is not None:
                    if label in labels:
                        raise ValueError(f"{where}.hostname: {label} is bound twice")
                    labels.add(label)
                    if label == APEX and self.deploy and self.deploy.domain == self.deploy.dns_zone:
                        raise ValueError(
                            f"{where}.hostname: @ is {self.deploy.domain}, the DNS zone itself, "
                            "whose apex records the zone keeps; choose a label"
                        )
                for route_name, route in binding.routes.items():
                    if route.access == "login" and not self.has("mcp"):
                        raise ValueError(
                            f"{where}.routes.{route_name}.access: a login route needs the mcp "
                            "component, whose sign-in admits people"
                        )
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

    def promotion_order(self) -> tuple[str, ...]:
        declared = self.deploy.promotion if self.deploy is not None else None
        return tuple(declared) if declared else DEFAULT_PROMOTION

    def promoted_from(self, env: str) -> str | None:
        """The environment `env` promotes from; None for the first one or one outside the order."""
        order = self.promotion_order()
        if env not in order:
            return None
        index = order.index(env)
        return order[index - 1] if index else None


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


def _merge(table: Any, data: dict[str, Any], changed: dict[str, Any]) -> None:
    """Make `table` hold `data`, touching only keys whose values differ.

    A key the file does not hold is added only when `changed` (the values that differ from
    their defaults) has it. Comments, blank lines and the order of everything unchanged stay as
    the person wrote them.
    """
    for key in [k for k in table if k not in data]:
        del table[key]
    for key, value in data.items():
        current = table.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _merge(current, value, changed.get(key) or {})
        elif current is None:
            if key in changed:
                table[key] = changed[key]
        elif _plain(current) != value:
            table[key] = value


def _plain(item: Any) -> Any:
    return item.unwrap() if hasattr(item, "unwrap") else item


def save_config(project_root: Path, config: VectenConfig) -> None:
    import tomlkit

    problem = promotion_scaffold_problem(config)
    if problem:
        raise Failure(ExitCode.REFUSED, problem)
    data = config.model_dump(mode="json", exclude_none=True)
    for optional in ("dependencies", "secrets", "settings", "services"):
        if not data.get(optional):
            data.pop(optional, None)
    path = project_root / CONFIG_FILE
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else None
    if doc is None:
        write_text_atomic(path, tomli_w.dumps(data))
        return
    changed = config.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    _merge(doc, data, changed)
    text = tomlkit.dumps(doc)
    # tomlkit can emit a table twice when a file declares one table's parts apart; such a file
    # does not load, so the whole configuration is written afresh instead, without comments.
    try:
        if VectenConfig.model_validate(tomllib.loads(text)) != config:
            raise ValueError("the edited file reads back differently")
    except tomllib.TOMLDecodeError, ValueError, ValidationError:
        text = tomli_w.dumps(data)
    write_text_atomic(path, text)


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
