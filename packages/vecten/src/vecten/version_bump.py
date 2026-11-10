"""The lockstep version bump of the four distributions, across every file that names it."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic

DISTRIBUTIONS = ("vecten", "vecten-platform", "vecten-mcp", "vecten-shared")
VERSIONS_TOML = "packages/vecten-shared/src/vecten_shared/versions.toml"
VERSION_FILES = (
    *(f"packages/{d}/pyproject.toml" for d in DISTRIBUTIONS),
    *(f"packages/{d}/src/{d.replace('-', '_')}/__init__.py" for d in DISTRIBUTIONS),
    VERSIONS_TOML,
    ".claude-plugin/marketplace.json",
    "plugins/vecten/.claude-plugin/plugin.json",
    "uv.lock",
)


@dataclass(frozen=True)
class Edit:
    rel: str
    text: str


def _rewrites(rel: str, old: str, new: str) -> list[tuple[str, str, bool]]:
    """(pattern, replacement, required) for one file; a required pattern matches exactly once."""
    o = re.escape(old)
    if rel.endswith("pyproject.toml"):
        return [
            (rf'^version = "{o}"$', f'version = "{new}"', True),
            (rf'("vecten(?:-[a-z]+)?==){o}"', rf'\g<1>{new}"', False),
        ]
    if rel.endswith("__init__.py"):
        return [(rf'^__version__ = "{o}"$', f'__version__ = "{new}"', True)]
    if rel == VERSIONS_TOML:
        return [
            (rf'^vecten = "{o}"$', f'vecten = "{new}"', True),
            (rf'^module_ref = "v{o}"$', f'module_ref = "v{new}"', True),
        ]
    if rel.endswith(".json"):
        return [(rf'"version": "{o}"', f'"version": "{new}"', True)]
    return [
        (rf'^(name = "{re.escape(d)}"\nversion = )"{o}"$', rf'\g<1>"{new}"', True)
        for d in DISTRIBUTIONS
    ]


def _owned(rel: str, text: str) -> str:
    """The part of a file where only this repository's version belongs.

    Third-party packages, the dbt package and the specifications carry versions of their own,
    which can equal ours.
    """
    if rel == "uv.lock":
        names = "|".join(map(re.escape, DISTRIBUTIONS))
        blocks = re.split(r"(?m)^(?=\[\[package\]\]$)", text)
        return "".join(b for b in blocks if re.search(rf'^name = "(?:{names})"$', b, re.M))
    if rel == VERSIONS_TOML:
        return re.sub(r"(?ms)^\[(?:dbt_packages|specs)\]$.*?(?=^\[|\Z)", "", text)
    if rel.endswith("pyproject.toml"):
        return "\n".join(line for line in text.splitlines() if "vecten" in line)
    return text


def current_version(root: Path) -> str:
    try:
        return tomllib.loads((root / VERSIONS_TOML).read_text(encoding="utf-8"))["vecten"]
    except OSError, KeyError, tomllib.TOMLDecodeError:
        raise Failure(
            ExitCode.USAGE, f"{root} is not a vecten-open checkout ({VERSIONS_TOML} unreadable)"
        ) from None


def check_target(old: str, new: str) -> None:
    try:
        target = Version(new)
    except InvalidVersion:
        raise Failure(ExitCode.USAGE, f"{new!r} is not a PEP 440 version") from None
    if str(target) != new:
        raise Failure(ExitCode.USAGE, f"write the version as {target}, its canonical form")
    if target <= Version(old):
        raise Failure(ExitCode.USAGE, f"{new} is not higher than the current version {old}")


def plan(root: Path, new: str) -> tuple[str, list[Edit]]:
    """Every file's bumped text; raises before any write when a file does not hold the version."""
    old = current_version(root)
    check_target(old, new)
    edits = []
    for rel in VERSION_FILES:
        path = root / rel
        if not path.is_file():
            raise Failure(ExitCode.CONFIG, f"{rel} is missing")
        text = path.read_text(encoding="utf-8")
        for pattern, replacement, required in _rewrites(rel, old, new):
            text, count = re.subn(pattern, replacement, text, flags=re.M)
            if required and count != 1:
                raise Failure(
                    ExitCode.CONFIG, f"{rel} does not hold version {old} where a bump expects it"
                )
        if re.search(rf"(?<![\d.]){re.escape(old)}(?!\.?\w)", _owned(rel, text)):
            raise Failure(ExitCode.CONFIG, f"{rel} names {old} where a bump does not rewrite it")
        edits.append(Edit(rel, text))
    return old, edits


def apply(root: Path, edits: list[Edit]) -> None:
    for edit in edits:
        write_text_atomic(root / edit.rel, edit.text)
