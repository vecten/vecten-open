"""Long-running services an extension declares in `vecten.services`, and their routes.

The declaration states facts about the code: the command, its port, its health check, the paths
it serves. `vecten lock` records it in `extensions.lock.json`, so `ship` in CI and `up` render
services without importing the extension. Choices about one deployment live in `vecten.toml`.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vecten_shared.names import group_name_problem
from vecten_shared.output import ExitCode, Failure

if TYPE_CHECKING:
    from vecten_shared.config import RouteBinding, ServiceBinding, VectenConfig
    from vecten_shared.extension_record import Extension

GROUP = "vecten.services"
# The `VECTEN_COMPONENT` of a service's container.
SERVICE_COMPONENT = "service"
NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
NAME_RULE = "a lowercase letter, then up to 30 lowercase letters, digits or hyphens"
SERVICE_KEYS = ("command", "port", "health", "volume", "components", "memory_mb", "cpus", "routes")
ROUTE_KEYS = ("path", "access", "groups", "websocket", "max_body_mb")
ACCESS_MODES = ("public", "login")
COMPONENTS = ("platform", "mcp")
RESERVED_PREFIX = "/_vecten"
DEFAULT_MEMORY_MB = 256
DEFAULT_CPUS = 0.5
MEMORY_MB = (64, 65536)
CPUS = (0.05, 64.0)
MAX_BODY_MB = (1, 100)
# Paths land unquoted in NGINX locations and in the health check's URL, so only characters
# with no meaning to either may pass.
_PATH = re.compile(r"/(?:[A-Za-z0-9._~-]+/?)*")
COMPOSE_NAME_LIMIT = 63


@dataclass(frozen=True)
class Route:
    path: str
    access: str
    groups: tuple[str, ...] = ()
    websocket: bool = False
    max_body_mb: int = 1


@dataclass(frozen=True)
class Service:
    command: tuple[str, ...]
    port: int | None
    health_path: str | None
    health_command: tuple[str, ...] | None
    volume: str | None = None
    components: tuple[str, ...] = ("platform",)
    memory_mb: int = DEFAULT_MEMORY_MB
    cpus: float = DEFAULT_CPUS
    routes: dict[str, Route] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Declaration:
    service: Service
    # Keys Vecten does not know, dotted below the service, such as `routes.hook.retries`.
    unknown: tuple[str, ...]


def name_problem(kind: str, name: object) -> str | None:
    if isinstance(name, str) and NAME.fullmatch(name):
        return None
    return f"{kind} {name!r} must be {NAME_RULE}"


def path_problem(value: object, *, prefix: bool) -> str | None:
    """Why `value` is not a path Vecten may render, or None."""
    if not isinstance(value, str) or not _PATH.fullmatch(value):
        return "must start with / and hold only letters, digits and . _ ~ - between slashes"
    segments = value.split("/")
    if "." in segments or ".." in segments:
        return "must not hold . or .. segments"
    if prefix and (value == RESERVED_PREFIX or value.startswith(RESERVED_PREFIX + "/")):
        return f"{RESERVED_PREFIX}/ is reserved for Vecten"
    return None


def route_prefix(path: str) -> str:
    """The segments a route path covers: `/api` and `/api/` both cover `/api` and below it."""
    return path.rstrip("/") or "/"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (_is_int(value) or isinstance(value, float)) and math.isfinite(value)


def _strings(value: object) -> tuple[str, ...] | None:
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return tuple(value)
    return None


def _route(fail, name: str, value: object) -> tuple[Route, list[str]]:
    where = f"routes.{name}"
    problem = name_problem("route", name)
    if problem:
        raise fail(where, problem)
    if not isinstance(value, dict):
        raise fail(where, "must be a mapping")
    problem = path_problem(value.get("path"), prefix=True)
    if problem:
        raise fail(f"{where}.path", problem)
    access = value.get("access")
    if access not in ACCESS_MODES:
        raise fail(f"{where}.access", f"must be one of {', '.join(ACCESS_MODES)}")
    groups = value.get("groups", [])
    if access == "login":
        if not isinstance(groups, list) or not groups:
            raise fail(f"{where}.groups", "a login route suggests one or more groups")
        for group in groups:
            problem = group_name_problem(group) if isinstance(group, str) else "must be a string"
            if problem:
                raise fail(f"{where}.groups", problem)
    elif groups != []:
        raise fail(f"{where}.groups", "only a login route names groups")
    websocket = value.get("websocket", False)
    if not isinstance(websocket, bool):
        raise fail(f"{where}.websocket", "must be true or false")
    body = value.get("max_body_mb", 1)
    if not _is_int(body) or not MAX_BODY_MB[0] <= body <= MAX_BODY_MB[1]:
        raise fail(f"{where}.max_body_mb", f"must be an integer from {MAX_BODY_MB[0]} to 100")
    unknown = [f"{where}.{k}" for k in value if k not in ROUTE_KEYS]
    return Route(value["path"], access, tuple(groups), websocket, body), unknown


def parse_service(extension: str, name: str, value: object) -> Declaration:
    """A `vecten.services` entry point's object, a mapping or a zero-argument factory."""

    def fail(key: str, problem: str) -> Failure:
        return Failure(ExitCode.CONFIG, f"extension {extension}: service {name}: {key}: {problem}")

    problem = name_problem("service", name)
    if problem:
        raise Failure(ExitCode.CONFIG, f"extension {extension}: {problem}")
    if callable(value):
        try:
            value = value()
        except Exception as exc:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {extension}: service {name}: its factory raised {type(exc).__name__}",
            ) from exc
    if not isinstance(value, dict):
        raise Failure(
            ExitCode.CONFIG,
            f"extension {extension}: service {name}: must be a mapping or return one",
        )
    return service_body(fail, value)


def service_body(fail, value: dict) -> Declaration:
    """A service mapping checked key by key; `fail(key, problem)` names where it came from."""
    command = _strings(value.get("command"))
    if command is None:
        raise fail("command", "a non-empty list of non-empty strings, run in exec form")
    port = value.get("port")
    if port is not None and (not _is_int(port) or not 1 <= port <= 65535):
        raise fail("port", "must be an integer from 1 to 65535")
    health = value.get("health")
    if not isinstance(health, dict) or len(set(health) & {"path", "command"}) != 1:
        raise fail("health", 'one of {"path": "/..."} or {"command": [...]}')
    health_path = health.get("path")
    if "path" in health:
        problem = path_problem(health_path, prefix=False)
        if problem:
            raise fail("health", f"path {problem}")
    health_command = _strings(health["command"]) if "command" in health else None
    if "command" in health and health_command is None:
        raise fail("health", "command must be a non-empty list of non-empty strings")
    volume = value.get("volume")
    if volume is not None:
        problem = path_problem(volume, prefix=False)
        if problem or volume.rstrip("/") == "":
            raise fail("volume", problem or "must not be /")
    components = value.get("components", ["platform"])
    if (
        not isinstance(components, list)
        or not components
        or not all(isinstance(c, str) for c in components)
        or not set(components) <= set(COMPONENTS)
    ):
        raise fail("components", f"a non-empty list of {', '.join(COMPONENTS)}")
    memory = value.get("memory_mb", DEFAULT_MEMORY_MB)
    if not _is_int(memory) or not MEMORY_MB[0] <= memory <= MEMORY_MB[1]:
        raise fail("memory_mb", f"must be an integer from {MEMORY_MB[0]} to {MEMORY_MB[1]}")
    cpus = value.get("cpus", DEFAULT_CPUS)
    if not _is_number(cpus) or not CPUS[0] <= cpus <= CPUS[1]:
        raise fail("cpus", f"must be a number from {CPUS[0]} to {CPUS[1]:g}")
    raw_routes = value.get("routes", {})
    if not isinstance(raw_routes, dict):
        raise fail("routes", "must be a mapping of route names")
    routes: dict[str, Route] = {}
    unknown = [k for k in value if k not in SERVICE_KEYS]
    unknown += [f"health.{k}" for k in health if k not in ("path", "command")]
    seen: set[str] = set()
    for route_name, route_value in raw_routes.items():
        route, extra = _route(fail, route_name, route_value)
        prefix = route_prefix(route.path)
        if prefix in seen:
            raise fail(f"routes.{route_name}.path", f"{prefix} is served by another route")
        seen.add(prefix)
        routes[route_name] = route
        unknown += extra
    if (routes or health_path is not None) and port is None:
        raise fail("port", "required when the service has routes or a health path")
    service = Service(
        command=command,
        port=port,
        health_path=health_path,
        health_command=health_command,
        volume=volume,
        components=tuple(sorted(set(components))),
        memory_mb=memory,
        cpus=float(cpus),
        routes=routes,
    )
    return Declaration(service, tuple(sorted(unknown)))


def service_to_json(service: Service) -> dict[str, Any]:
    return {
        "command": list(service.command),
        "port": service.port,
        "health": (
            {"path": service.health_path}
            if service.health_path is not None
            else {"command": list(service.health_command or ())}
        ),
        "volume": service.volume,
        "components": list(service.components),
        "memory_mb": service.memory_mb,
        "cpus": service.cpus,
        "routes": {
            name: {
                "path": route.path,
                "access": route.access,
                "groups": list(route.groups),
                "websocket": route.websocket,
                "max_body_mb": route.max_body_mb,
            }
            for name, route in sorted(service.routes.items())
        },
    }


def service_from_json(value: object) -> Service:
    """The recorded form, checked as strictly as a declaration; ValueError when it is not."""
    if not isinstance(value, dict):
        raise ValueError("a service record is a mapping")
    try:
        return parse_service("record", "recorded", value).service
    except Failure as failure:
        raise ValueError(failure.message) from None


def compose_service_name(extension: str, service: str) -> str:
    name = f"svc-{extension}-{service}"
    if len(name) <= COMPOSE_NAME_LIMIT:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[: COMPOSE_NAME_LIMIT - 9]}-{digest}"


@dataclass(frozen=True)
class Bound:
    """A declared service the project binds, with its deployment choices resolved."""

    extension: str
    name: str
    service: Service
    binding: ServiceBinding
    host: str
    memory_mb: int
    cpus: float
    # Only the routes the project binds, as declared.
    routes: dict[str, Route] = field(hash=False)
    route_bindings: dict[str, RouteBinding] = field(hash=False)
    # The analytics app this service runs, for the service a container app generates.
    app: str | None = None

    @property
    def compose_name(self) -> str:
        return compose_service_name(self.extension, self.name)

    @property
    def key(self) -> str:
        return f"{self.extension}.{self.name}"

    @property
    def described(self) -> str:
        return f"app {self.extension}.{self.app}" if self.app else self.key


def _host(config: VectenConfig, binding: ServiceBinding, service: Service) -> str | None:
    if binding.host is not None:
        return binding.host
    if "platform" in service.components and config.has("platform"):
        return "platform"
    return None


def _fail(where: str, problem: str) -> Failure:
    return Failure(ExitCode.CONFIG, f"{where}: {problem}")


def check_bindings(config: VectenConfig, record: dict[str, Extension]) -> None:
    """Every binding names a declared service and route, on a host its declaration allows."""
    bound_services(config, record)


def bound_services(config: VectenConfig, record: dict[str, Extension]) -> list[Bound]:
    found: list[Bound] = []
    for extension, bindings in sorted(config.services.items()):
        entry = record.get(extension)
        if entry is None:
            raise _fail(
                f"services.{extension}",
                f"{extension} is not in extensions.lock.json yet; run `vecten lock`",
            )
        for name, binding in sorted(bindings.items()):
            where = f"services.{extension}.{name}"
            service = entry.services.get(name)
            if service is None:
                raise _fail(where, f"{extension} declares no service {name}")
            for route in binding.routes:
                if route not in service.routes:
                    raise _fail(
                        f"{where}.routes.{route}", f"service {name} declares no route {route}"
                    )
            host = _host(config, binding, service)
            allowed = ", ".join(service.components)
            if host is None:
                raise _fail(f"{where}.host", f"required; {name} runs from {allowed}")
            if host not in service.components:
                raise _fail(f"{where}.host", f"{name} runs only from {allowed}")
            found.append(
                Bound(
                    extension=extension,
                    name=name,
                    service=service,
                    binding=binding,
                    host=host,
                    memory_mb=binding.memory_mb or service.memory_mb,
                    cpus=binding.cpus if binding.cpus is not None else service.cpus,
                    routes={r: service.routes[r] for r in sorted(binding.routes)},
                    route_bindings=dict(sorted(binding.routes.items())),
                )
            )
    from vecten_shared.apps import bound_apps

    found += bound_apps(config, record, {b.binding.hostname for b in found} - {None})
    # Compose keeps one service, and one volume, per name.
    named: dict[str, str] = {}
    for bound in found:
        if bound.compose_name in named:
            raise Failure(
                ExitCode.CONFIG,
                f"services: {named[bound.compose_name]} and {bound.described} share the Compose "
                f"name {bound.compose_name}; rename one service",
            )
        named[bound.compose_name] = bound.described
    return found


def unbound_parts(config: VectenConfig, record: dict[str, Extension]) -> list[str]:
    """Services, routes and apps listed extensions declare that `vecten.toml` does not bind."""
    from vecten_shared.requirements import parse_requirement
    from vecten_shared.versions import normalize_name

    listed = {normalize_name(r.name) for r in map(parse_requirement, config.extensions) if r}
    found = []
    for extension, entry in sorted(record.items()):
        if extension not in listed:
            continue
        bindings = config.services.get(extension, {})
        for name, service in sorted(entry.services.items()):
            binding = bindings.get(name)
            if binding is None:
                found.append(f"{extension}.{name}")
                continue
            found += [
                f"{extension}.{name} route {route}"
                for route in sorted(service.routes)
                if route not in binding.routes
            ]
        apps = config.apps.get(extension, {})
        found += [f"{extension} app {name}" for name in sorted(entry.apps) if name not in apps]
    return found


def limits_per_host(config: VectenConfig, record: dict[str, Extension]) -> dict[str, dict]:
    """What bound services reserve on each deployed host: one VM under `minimal`."""
    found: dict[str, dict] = {}
    for bound in bound_services(config, record):
        host = "vm" if config.profile == "minimal" else bound.host
        entry = found.setdefault(host, {"memory_mb": 0, "cpus": 0.0})
        entry["memory_mb"] += bound.memory_mb
        entry["cpus"] = round(entry["cpus"] + bound.cpus, 2)
    return found
