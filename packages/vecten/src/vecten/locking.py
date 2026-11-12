"""What each component's lock is resolved from, and whether a committed lock still matches it.

Everything here reads committed files only, so `up`, the bundle build and `next` can tell a
stale lock without the network, Docker or the extensions being installed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from vecten_shared.config import VectenConfig
from vecten_shared.requirements import parse_requirement
from vecten_shared.versions import VERSIONS, normalize_name

LOCK_FILES = {
    "cli": "requirements.lock",
    "platform": "requirements-airflow.lock",
    "mcp": "requirements-mcp.lock",
}
RECORD_FILE = "extensions.lock.json"
PINS_FILE = "requirements.txt"
STAMP = "# vecten-lock:"
# The base image a runtime lock is resolved against; the CLI's lock has none.
BASE_IMAGES = {"platform": "vecten-airflow", "mcp": "vecten-mcp"}


@dataclass(frozen=True)
class Extension:
    version: str
    extras: tuple[str, ...]


def release() -> str:
    return VERSIONS.vecten


def components(config: VectenConfig) -> list[str]:
    return ["cli", *(c for c in ("platform", "mcp") if config.has(c))]


def read_pins(root: Path) -> dict[str, str]:
    """Exact pins from `requirements.txt`, by normalised name; other lines are ignored."""
    path = root / PINS_FILE
    if not path.is_file():
        return {}
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        req = parse_requirement(line.strip()) if line.strip() and not line.startswith("#") else None
        if req is not None:
            found[normalize_name(req.name)] = str(req)
    return found


def _exact_version(text: str) -> str | None:
    try:
        specs = list(Requirement(text).specifier)
    except InvalidRequirement:
        return None
    if len(specs) == 1 and specs[0].operator == "==" and "*" not in specs[0].version:
        return specs[0].version
    return None


def roots(
    config: VectenConfig, pins: dict[str, str], record: dict[str, Extension], component: str
) -> list[str] | None:
    """The requirements `component`'s lock is resolved from, or None while one is unknown.

    An extension's extra for the component is requested only when its wheel provides it, so
    a wheel without extras locks as it always has.
    """
    found = set()
    for registration in config.extensions:
        req = Requirement(registration)
        name = normalize_name(req.name)
        version = _exact_version(pins[name]) if name in pins else None
        entry = record.get(name)
        if version is None or entry is None or entry.version != version:
            return None
        extras = set(req.extras) | ({component} & set(entry.extras))
        listed = f"[{','.join(sorted(extras))}]" if extras else ""
        found.add(f"{name}{listed}=={version}")
    for connector in config.connectors:
        found.update(connector.dependencies)
    found.update(config.dependencies.python(component))
    if component == "cli":
        found.add(f"vecten=={release()}")
    return sorted(found)


def inputs_hash(component: str, lock_roots: list[str]) -> str:
    base = BASE_IMAGES.get(component)
    inputs = {
        "component": component,
        "roots": sorted(lock_roots),
        "base": f"{base}:{release()}" if base else None,
        "release": release(),
        "python": VERSIONS.python,
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def stamp_line(component: str, digest: str) -> str:
    return f"{STAMP} component={component} inputs={digest}"


def read_stamp(text: str) -> tuple[str, str] | None:
    first = text.split("\n", 1)[0]
    if not first.startswith(STAMP):
        return None
    fields = dict(part.split("=", 1) for part in first[len(STAMP) :].split() if "=" in part)
    if "component" not in fields or "inputs" not in fields:
        return None
    return fields["component"], fields["inputs"]


def record_text(record: dict[str, Extension]) -> str:
    body = {
        "version": 1,
        "extensions": {
            name: {"version": entry.version, "extras": sorted(entry.extras)}
            for name, entry in sorted(record.items())
        },
    }
    return json.dumps(body, indent=2) + "\n"


def parse_record(text: str) -> dict[str, Extension] | None:
    """The record, or None when any part is malformed: a partial record is never trusted."""
    try:
        entries = json.loads(text)["extensions"].items()
        record = {}
        for name, entry in entries:
            version, extras = entry["version"], entry["extras"]
            if not isinstance(version, str) or not all(isinstance(e, str) for e in extras):
                return None
            record[name] = Extension(version=version, extras=tuple(sorted(extras)))
        return record
    except ValueError, KeyError, TypeError, AttributeError:
        return None


def read_record(root: Path) -> dict[str, Extension] | None:
    path = root / RECORD_FILE
    if not path.is_file():
        return None
    return parse_record(path.read_text(encoding="utf-8"))


def lock_state(root: Path, config: VectenConfig, component: str) -> str:
    """`fresh`, `stale`, `unstamped` (an earlier release wrote it) or `missing`."""
    path = root / LOCK_FILES[component]
    if not path.is_file():
        return "missing"
    stamp = read_stamp(path.read_text(encoding="utf-8"))
    if stamp is None:
        return "unstamped"
    record = read_record(root)
    lock_roots = roots(config, read_pins(root), record or {}, component) if record else None
    if lock_roots is None or stamp != (component, inputs_hash(component, lock_roots)):
        return "stale"
    return "fresh"


def lock_problems(root: Path, config: VectenConfig) -> list[str]:
    """The records `vecten lock` must rewrite before this project can build."""
    problems = []
    if read_record(root) is None:
        problems.append(RECORD_FILE)
    for component in components(config):
        if lock_state(root, config, component) != "fresh":
            problems.append(LOCK_FILES[component])
    return problems


def lock_pins(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        version = _exact_version(line) if line.strip() and not line.startswith("#") else None
        if version is not None:
            found[normalize_name(Requirement(line).name)] = version
    return found


def divergence(locks: dict[str, str]) -> dict[str, dict[str, str]]:
    """Packages pinned at different versions in different locks, by lock."""
    by_package: dict[str, dict[str, str]] = {}
    for component, text in locks.items():
        for name, version in lock_pins(text).items():
            by_package.setdefault(name, {})[component] = version
    return {
        name: dict(sorted(versions.items()))
        for name, versions in sorted(by_package.items())
        if len(set(versions.values())) > 1
    }
