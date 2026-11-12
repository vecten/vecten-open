"""What each component's lock is resolved from, and whether a committed lock still matches it.

Everything here reads committed files only, so `up`, the bundle build and `next` can tell a
stale lock without the network, Docker or the extensions being installed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import (
    RECORD_FILE,
    Extension,
    parse_record,
    read_record,
    record_text,
)
from vecten_shared.requirements import parse_requirement
from vecten_shared.versions import VERSIONS, normalize_name

# The record lives in vecten-shared because the runtimes read it; it is re-exported here for the
# CLI's locking code.
__all__ = [
    "LOCK_FILES",
    "PINS_FILE",
    "RECORD_FILE",
    "Extension",
    "components",
    "divergence",
    "inputs_hash",
    "lock_problems",
    "lock_state",
    "listed_paths",
    "metadata_current",
    "paths_listed",
    "metadata_markers",
    "parse_record",
    "read_pins",
    "read_record",
    "read_stamp",
    "record_text",
    "roots",
    "stamp_line",
]

LOCK_FILES = {
    "cli": "requirements.lock",
    "platform": "requirements-airflow.lock",
    "mcp": "requirements-mcp.lock",
}
PINS_FILE = "requirements.txt"
STAMP = "# vecten-lock:"
# The base image a runtime lock is resolved against; the CLI's lock has none.
BASE_IMAGES = {"platform": "vecten-airflow", "mcp": "vecten-mcp"}


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
        req = parse_requirement(registration)
        if req is None:
            return None
        name = normalize_name(req.name)
        entry = record.get(name)
        if entry is not None and entry.source_path:
            # uv resolves the path against its working directory, the project root.
            extras = set(req.extras) | ({component} & set(entry.extras))
            listed = f"[{','.join(sorted(extras))}]" if extras else ""
            found.add(f"./{entry.source_path}{listed}")
            continue
        version = _exact_version(pins[name]) if name in pins else None
        # The runtimes refuse an installed version outside the registration's constraint.
        if version is None or entry is None or entry.version != version:
            return None
        if req.specifier and version not in req.specifier:
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


def metadata_markers(record: dict[str, Extension]) -> list[str]:
    """One input per in-repo extension: its pyproject.toml's hash as `vecten lock` read it.

    Its metadata decides what resolves; its code does not, so only this joins a lock's inputs.
    """
    return sorted(
        f"{name} metadata={entry.metadata_sha256}"
        for name, entry in record.items()
        if entry.source_path
    )


def metadata_current(root: Path, record: dict[str, Extension]) -> bool:
    """Whether every in-repo extension's pyproject.toml is still the one the record hashed."""
    for entry in record.values():
        if not entry.source_path:
            continue
        try:
            digest = hashlib.sha256((root / entry.source_path / "pyproject.toml").read_bytes())
        except OSError:
            return False
        if digest.hexdigest() != entry.metadata_sha256:
            return False
    return True


def listed_paths(root: Path) -> set[str]:
    """The `./extensions/<name>` lines of requirements.txt, as project-relative paths."""
    path = root / PINS_FILE
    if not path.is_file():
        return set()
    return {
        line[2:].split("[")[0].rstrip("/")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("./extensions/")
    }


def paths_listed(root: Path, record: dict[str, Extension]) -> bool:
    """Whether requirements.txt still lists every in-repo extension by its path."""
    listed = listed_paths(root)
    return all(e.source_path in listed for e in record.values() if e.source_path)


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


def lock_state(root: Path, config: VectenConfig, component: str) -> str:
    """`fresh`, `stale`, `unstamped` (an earlier release wrote it) or `missing`."""
    path = root / LOCK_FILES[component]
    if not path.is_file():
        return "missing"
    stamp = read_stamp(path.read_text(encoding="utf-8"))
    if stamp is None:
        return "unstamped"
    record = read_record(root)
    lock_roots = None if record is None else roots(config, read_pins(root), record, component)
    if lock_roots is None or not metadata_current(root, record) or not paths_listed(root, record):
        return "stale"
    digest = inputs_hash(component, [*lock_roots, *metadata_markers(record)])
    return "fresh" if stamp == (component, digest) else "stale"


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
