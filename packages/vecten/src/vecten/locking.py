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
    "git_problems",
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
    """Exact pins and git source lines from `requirements.txt`, by normalised name; other
    lines are ignored. A git source's line is kept as written, with its `# tag` comment."""
    from vecten import git_extensions

    path = root / PINS_FILE
    if not path.is_file():
        return {}
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if git_extensions.is_source(line):
            name = git_extensions.line_name(line)
            if name is not None:
                found[name] = line.strip()
            continue
        req = parse_requirement(line.strip())
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
        if entry is not None and entry.git is not None:
            from vecten import git_extensions

            if pins.get(name) != git_extensions.line_for(name, req.extras, entry.git):
                return None
            extras = set(req.extras) | ({component} & set(entry.extras))
            found.add(git_extensions.root_line(name, extras, entry.git))
            continue
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


def _source_lines(text: str) -> dict[str | None, list[str]]:
    from vecten import git_extensions

    found: dict[str | None, list[str]] = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#") and git_extensions.is_source(line):
            found.setdefault(git_extensions.line_name(line), []).append(line.strip())
    return found


def git_problems(root: Path, config: VectenConfig) -> list[str]:
    """How `requirements.txt`, the locks and the record disagree about git sources.

    Only a registered extension may carry a URL, a full commit of a `git+https` source with no
    user information, the same line in `requirements.txt` and every lock, agreeing with the
    record. A refusal names the extension and the file, never the URL.
    """
    from vecten import git_extensions
    from vecten_shared.output import Failure

    record = read_record(root) or {}
    registered = {}
    for registration in config.extensions:
        req = parse_requirement(registration)
        if req is not None:
            registered[normalize_name(req.name)] = req
    files = [PINS_FILE, *(LOCK_FILES[c] for c in components(config))]
    problems = []
    expected = {
        name: git_extensions.line_for(name, registered[name].extras, entry.git)
        for name, entry in record.items()
        if entry.git is not None and name in registered
    }
    for file in files:
        path = root / file
        lines = _source_lines(path.read_text(encoding="utf-8")) if path.is_file() else {}
        for name, found in sorted(lines.items(), key=lambda item: item[0] or ""):
            shown = name or "a line"
            if name not in registered:
                problems.append(f"{file}: {shown} comes from a URL but is not registered")
                continue
            if len(found) > 1:
                problems.append(f"{file}: {name} has more than one line")
            for line in found:
                try:
                    git_extensions.parse_line(line, file)
                except Failure as failure:
                    problems.append(failure.message)
                    continue
                if name not in expected:
                    problems.append(f"{file}: {name} comes from git, but {RECORD_FILE} says not")
                elif line != expected[name]:
                    problems.append(f"{file}: the git source of {name} differs from {RECORD_FILE}")
        for name in sorted(set(expected) - set(lines)):
            problems.append(f"{file}: {name} lacks the git source {RECORD_FILE} records")
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
