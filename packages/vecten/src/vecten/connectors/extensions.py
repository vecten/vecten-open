"""Connectors that installed extensions offer through the `vecten.connectors` entry point group."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from zoneinfo import ZoneInfo

from vecten_shared.config import SOURCE_KINDS, VectenConfig
from vecten_shared.extensions import selected_entry_points
from vecten_shared.names import SECRET_NAME, SECRET_RULE, group_name_problem
from vecten_shared.output import ExitCode, Failure

GROUP = "vecten.connectors"
DEFAULT_SCHEDULE = "@daily"
_GROUP_KEYS = {"objects", "schedule", "timezone"}


@dataclass(frozen=True)
class ExtensionGroup:
    """One object group an extension declares: its objects and the schedule of its DAG."""

    objects: list[str]
    schedule: str = DEFAULT_SCHEDULE
    timezone: str | None = None


@dataclass(frozen=True)
class ExtensionConnector:
    name: str
    package: str
    version: str
    kind: str
    secrets: list[str]
    dependencies: list[str]
    write: Callable[[Path], None]
    # Empty when the extension declares no groups: the connector gets the one default group.
    groups: dict[str, ExtensionGroup] = field(default_factory=dict)


def entry_points_for(group: str):
    return list(entry_points(group=group))


def _load_spec(ep, package: str) -> dict:
    where = f"extension {package}, entry point {ep.name}"
    try:
        factory = ep.load()
        spec = factory() if callable(factory) else factory
    except Exception as exc:
        raise Failure(
            ExitCode.CONFIG, f"{where} failed to load: {type(exc).__name__}: {exc}"
        ) from None
    if not isinstance(spec, dict):
        raise Failure(ExitCode.CONFIG, f"{where} must provide a dict, got {type(spec).__name__}")
    if not callable(spec.get("write")):
        raise Failure(ExitCode.CONFIG, f"{where} has no callable write")
    return spec


def _string_list(spec: dict, key: str, package: str, connector: str) -> list[str]:
    # A bare string would otherwise split into one entry per character.
    value = spec.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Failure(
            ExitCode.CONFIG,
            f"extension {package} declares connector {connector} with {key} that is not a "
            f"list of strings: {value!r}",
        )
    return list(value)


def _refuse(package: str, connector: str, detail: str) -> Failure:
    return Failure(
        ExitCode.CONFIG, f"extension {package} declares connector {connector} with {detail}"
    )


def _group(value: object, name: str, package: str, connector: str) -> ExtensionGroup:
    where = f"group {name}"
    # A bare list is the objects alone, the shape `groups` has in vecten.toml.
    spec = {"objects": value} if isinstance(value, list) else value
    if not isinstance(spec, dict):
        raise _refuse(package, connector, f"{where} that is neither a dict nor a list: {value!r}")
    unknown = sorted(set(spec) - _GROUP_KEYS)
    if unknown:
        raise _refuse(package, connector, f"{where} with unknown keys {unknown!r}")
    objects = _string_list(spec, "objects", package, f"{connector} {where}")
    schedule = spec.get("schedule", DEFAULT_SCHEDULE)
    # The schedule and zone are rendered into the DAG file as string literals.
    if not isinstance(schedule, str) or not schedule.strip() or not schedule.isprintable():
        raise _refuse(package, connector, f"{where} whose schedule is not a cron string")
    timezone = spec.get("timezone")
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except TypeError, ValueError, LookupError:
            raise _refuse(package, connector, f"{where} in unknown timezone {timezone!r}") from None
    return ExtensionGroup(objects=objects, schedule=schedule.strip(), timezone=timezone)


def _groups(spec: dict, package: str, connector: str) -> dict[str, ExtensionGroup]:
    value = spec.get("groups")
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        raise _refuse(package, connector, f"groups that is not a dict of groups: {value!r}")
    groups = {}
    for name, group in value.items():
        # The group names the DAG file `sync_<connector>_<group>.py` and the DAG id.
        problem = group_name_problem(name) if isinstance(name, str) else "a group name is text"
        if problem:
            raise _refuse(package, connector, f"group {name!r}: {problem}")
        groups[name] = _group(group, name, package, connector)
    return groups


def extension_connectors(config: VectenConfig) -> dict[str, ExtensionConnector]:
    found: dict[str, ExtensionConnector] = {}
    for ep in selected_entry_points(config, GROUP, entry_points_for(GROUP)):
        dist = getattr(ep, "dist", None)
        package = dist.name if dist is not None else ""
        spec = _load_spec(ep, package)
        kind = spec.get("kind", "custom")
        if kind not in SOURCE_KINDS:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {package} declares connector {ep.name} with unknown kind {kind!r}",
            )
        secrets = _string_list(spec, "secrets", package, ep.name)
        dependencies = _string_list(spec, "dependencies", package, ep.name)
        bad = [s for s in secrets if not SECRET_NAME.fullmatch(s)]
        if bad:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {package} declares connector {ep.name} with invalid secret names "
                f"{bad!r}; a secret name is {SECRET_RULE}",
            )
        found[ep.name] = ExtensionConnector(
            name=ep.name,
            package=package,
            version=getattr(dist, "version", "") or "",
            kind=kind,
            secrets=secrets,
            dependencies=dependencies,
            write=spec["write"],
            groups=_groups(spec, package, ep.name),
        )
    return found
