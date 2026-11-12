"""Analytics apps an extension declares in `vecten.apps`.

The declaration states what the app is and which Ossie datasets it reads. `vecten lock` records
it in `extensions.lock.json`. A `container` app runs as the extension service
`svc-<extension>-app-<app>`, generated here from the declaration, with one `login` route at `/`;
a `snowflake` app runs inside the warehouse and renders nothing on a host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vecten_shared.names import group_name_problem
from vecten_shared.output import ExitCode, Failure
from vecten_shared.services import (
    COMPONENTS,
    Bound,
    Service,
    name_problem,
    service_body,
    service_to_json,
)

if TYPE_CHECKING:
    from vecten_shared.config import VectenConfig
    from vecten_shared.extension_record import Extension

GROUP = "vecten.apps"
KINDS = ("container", "snowflake")
RUNTIMES = ("streamlit",)
APP_KEYS = (
    "title",
    "description",
    "kind",
    "runtime",
    "entry",
    "command",
    "port",
    "health",
    "datasets",
    "groups",
    "components",
    "memory_mb",
    "cpus",
)
CONTAINER_KEYS = ("runtime", "command", "port", "health", "components", "memory_mb", "cpus")
# The route a container app's service serves, at `/`, to the groups its binding names.
ROUTE = "app"
STREAMLIT_PORT = 8501
STREAMLIT_HEALTH = "/_stcore/health"
LAUNCHER = ("python", "-m", "vecten_shared.launch")
# A dashboard process holds its data frames in memory; a service's 256 MB default is too small.
DEFAULT_MEMORY_MB = 512
DEFAULT_HEALTH = {"path": "/"}
_DATASET = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
# The names `vecten app connect` stores an app principal's client credentials under; the product
# delivers them to that app's service only, so no declaration may claim one.
APP_CREDENTIAL = re.compile(r"(?:MCP_)?APP_[A-Z0-9_]+_CLIENT_(?:ID|SECRET)")


@dataclass(frozen=True)
class App:
    title: str
    description: str
    kind: str
    datasets: tuple[str, ...]
    groups: tuple[str, ...]
    entry: str | None = None
    runtime: str | None = None
    # A container app's generated service; None for a snowflake app.
    service: Service | None = None


@dataclass(frozen=True)
class AppDeclaration:
    app: App
    # Keys Vecten does not know, dotted below the app, such as `health.every`.
    unknown: tuple[str, ...]


def entry_problem(value: object) -> str | None:
    """Why `value` is not `<package>/<path>.py` inside an installed package, or None."""
    if not isinstance(value, str):
        return "a path such as acme_sales/app.py inside the installed package"
    segments = value.split("/")
    if (
        len(segments) < 2
        or not _IDENTIFIER.fullmatch(segments[0])
        or not all(_SEGMENT.fullmatch(s) and s not in (".", "..") for s in segments)
        or not value.endswith(".py")
    ):
        return (
            "a .py file inside the installed package, as <package>/<path>, with no . or .. segments"
        )
    return None


def _called(extension: str, name: str, value: object) -> dict:
    if callable(value):
        try:
            value = value()
        except Exception as exc:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {extension}: app {name}: its factory raised {type(exc).__name__}",
            ) from exc
    if not isinstance(value, dict):
        raise Failure(
            ExitCode.CONFIG, f"extension {extension}: app {name}: must be a mapping or return one"
        )
    return value


def parse_app(extension: str, name: str, value: object) -> AppDeclaration:
    """A `vecten.apps` entry point's object, a mapping or a zero-argument factory."""

    def fail(key: str, problem: str) -> Failure:
        return Failure(ExitCode.CONFIG, f"extension {extension}: app {name}: {key}: {problem}")

    problem = name_problem("app", name)
    if problem:
        raise Failure(ExitCode.CONFIG, f"extension {extension}: {problem}")
    value = _called(extension, name, value)
    title, description = value.get("title"), value.get("description")
    if not isinstance(title, str) or not title.strip():
        raise fail("title", "a non-empty string")
    if not isinstance(description, str):
        raise fail("description", "a string")
    kind = value.get("kind")
    if kind not in KINDS:
        raise fail("kind", f"must be one of {', '.join(KINDS)}")
    datasets = value.get("datasets")
    if (
        not isinstance(datasets, list)
        or not datasets
        or not all(isinstance(d, str) and _DATASET.fullmatch(d) for d in datasets)
        or len(set(datasets)) != len(datasets)
    ):
        raise fail("datasets", "a non-empty list of distinct Ossie dataset names")
    groups = value.get("groups")
    if not isinstance(groups, list) or not groups:
        raise fail("groups", "suggest one or more MCP groups that may open the app")
    for group in groups:
        problem = group_name_problem(group) if isinstance(group, str) else "must be a string"
        if problem:
            raise fail("groups", problem)
    unknown = [k for k in value if k not in APP_KEYS]
    common = {
        "title": title,
        "description": description,
        "kind": kind,
        "datasets": tuple(datasets),
        "groups": tuple(groups),
    }
    if kind == "snowflake":
        for key in CONTAINER_KEYS:
            if key in value:
                raise fail(key, "only a container app gives it")
        problem = entry_problem(value.get("entry"))
        if problem:
            raise fail("entry", problem)
        return AppDeclaration(App(**common, entry=value["entry"]), tuple(sorted(unknown)))
    runtime, entry = value.get("runtime"), value.get("entry")
    service = {k: value[k] for k in ("health", "components", "cpus") if k in value}
    service["memory_mb"] = value.get("memory_mb", DEFAULT_MEMORY_MB)
    if "runtime" in value:
        if runtime not in RUNTIMES:
            raise fail("runtime", f"must be one of {', '.join(RUNTIMES)}, or give command and port")
        for key in ("command", "port", "health"):
            if key in value:
                raise fail(key, f"runtime {runtime} sets it")
        problem = entry_problem(entry)
        if problem:
            raise fail("entry", problem)
        service |= {
            "command": [*LAUNCHER, runtime, entry],
            "port": STREAMLIT_PORT,
            "health": {"path": STREAMLIT_HEALTH},
        }
    else:
        if "entry" in value:
            raise fail("entry", "only runtime streamlit or a snowflake app gives it")
        if "command" not in value:
            raise fail("command", "give runtime streamlit with entry, or command and port")
        if "port" not in value:
            raise fail("port", "required with command")
        service |= {"command": value["command"], "port": value["port"]}
        service.setdefault("health", DEFAULT_HEALTH)
    route = {"path": "/", "access": "login", "groups": groups, "websocket": True}
    service["routes"] = {ROUTE: route}
    declared = service_body(fail, service)
    unknown += [k for k in declared.unknown if k.startswith("health.")]
    app = App(**common, entry=entry, runtime=runtime, service=declared.service)
    return AppDeclaration(app, tuple(sorted(unknown)))


def app_to_json(app: App) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": app.title,
        "description": app.description,
        "kind": app.kind,
        "datasets": list(app.datasets),
        "groups": list(app.groups),
    }
    if app.entry is not None:
        body["entry"] = app.entry
    if app.service is None:
        return body
    service = service_to_json(app.service)
    if app.runtime is not None:
        body["runtime"] = app.runtime
    else:
        body |= {k: service[k] for k in ("command", "port", "health")}
    return body | {k: service[k] for k in ("components", "memory_mb", "cpus")}


def app_from_json(name: str, value: object) -> App:
    """The recorded form, checked as strictly as a declaration; ValueError when it is not."""
    if not isinstance(value, dict):
        raise ValueError("an app record is a mapping")
    try:
        return parse_app("record", name, value).app
    except Failure as failure:
        raise ValueError(failure.message) from None


def app_service_name(app: str) -> str:
    return f"app-{app}"


def colliding_service(services: dict[str, object], apps: dict[str, object]) -> str | None:
    """A declared service named as one of the extension's apps' generated services."""
    return next((s for s in map(app_service_name, sorted(apps)) if s in services), None)


def principal_name(app: str) -> str:
    """The warehouse name of app `app`'s principal: every run of characters other than
    lowercase letters and digits becomes one `_`, so `a-b` and `a--b` share it."""
    return "svc_app_" + re.sub(r"[^a-z0-9]+", "_", app.lower()).strip("_")


def credential_names(app: str, host: str) -> tuple[str, str]:
    """The client id and secret names an app's principal is stored under, in its host's scope."""
    stem = ("MCP_" if host == "mcp" else "") + "APP_" + app.upper().replace("-", "_")
    return f"{stem}_CLIENT_ID", f"{stem}_CLIENT_SECRET"


def _fail(where: str, problem: str) -> Failure:
    return Failure(ExitCode.CONFIG, f"{where}: {problem}")


def bound_apps(config: VectenConfig, record: dict[str, Extension], taken: set[str]) -> list[Bound]:
    """The service each bound container app runs as; `taken` holds the hostname labels bound
    services use, and gains each app's."""
    from vecten_shared.config import RouteBinding, ServiceBinding, label_problem

    found: list[Bound] = []
    for extension, bindings in sorted(config.apps.items()):
        entry = record.get(extension)
        if entry is None:
            raise _fail(
                f"apps.{extension}",
                f"{extension} is not in extensions.lock.json yet; run `vecten lock`",
            )
        for name, binding in sorted(bindings.items()):
            where = f"apps.{extension}.{name}"
            app = entry.apps.get(name)
            if app is None:
                raise _fail(where, f"{extension} declares no app {name}")
            service = app.service
            if service is None:
                for key in ("hostname", "local"):
                    if key in binding.model_fields_set:
                        raise _fail(f"{where}.{key}", "only a container app runs on a host")
                continue
            label = binding.hostname or name
            problem = label_problem(label)
            if problem:
                raise _fail(f"{where}.hostname", f"{problem}; it defaults to the app's name")
            if label in taken:
                raise _fail(f"{where}.hostname", f"hostname {label} is bound twice")
            taken.add(label)
            host = next((c for c in COMPONENTS if c in service.components and config.has(c)), None)
            if host is None:
                raise _fail(
                    where,
                    f"runs from {', '.join(service.components)}, which the project does not enable",
                )
            routes = {ROUTE: RouteBinding(access="login", groups=list(binding.groups))}
            found.append(
                Bound(
                    extension=extension,
                    name=app_service_name(name),
                    service=service,
                    binding=ServiceBinding(hostname=label, local=binding.local, routes=routes),
                    host=host,
                    memory_mb=service.memory_mb,
                    cpus=service.cpus,
                    routes=dict(service.routes),
                    route_bindings=routes,
                    app=name,
                )
            )
    return found
