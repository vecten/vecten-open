"""In-repo extensions: packages kept in the project under `extensions/<name>/`.

Only that folder is accepted, so the bundle carries every in-repo extension as one entry and a
path can never point outside the project, at another machine's checkout, or through a link at
files the bundle would not carry.
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import normalize_name

EXTENSIONS_DIR = "extensions"
RULE = "in-repo extensions live under extensions/<name>/ in the project"


@dataclass(frozen=True)
class InRepo:
    name: str
    version: str
    # The project-relative POSIX path, `extensions/<name>`.
    path: str
    extras: tuple[str, ...]
    groups: tuple[str, ...]
    build_requires: tuple[str, ...]
    metadata_sha256: str


def is_path(arg: str) -> bool:
    """Whether `arg` names a directory rather than a package; package names hold no `/`."""
    return "/" in arg or arg.startswith(".")


def _refuse(problem: str) -> Failure:
    return Failure(ExitCode.USAGE, f"{problem}; {RULE}")


def _relative(root: Path, arg: str) -> PurePosixPath:
    if ".." in PurePosixPath(arg).parts:
        raise _refuse(f"{arg} holds a .. component")
    given = Path(arg)
    if given.is_absolute():
        # macOS reaches /tmp through /private, so the project may be named either way.
        for base in (root, root.resolve()):
            if given.is_relative_to(base):
                given = given.relative_to(base)
                break
        else:
            raise _refuse(f"{arg} is outside the project")
    relative = PurePosixPath(*given.parts)
    if relative.parts and relative.parts[0] == ".":
        relative = PurePosixPath(*relative.parts[1:])
    if len(relative.parts) != 2 or relative.parts[0] != EXTENSIONS_DIR:
        raise _refuse(f"{arg} is not a directory directly under extensions/")
    return relative


def _check_links(root: Path, relative: PurePosixPath) -> None:
    here = root
    for part in relative.parts:
        here = here / part
        if here.is_symlink():
            raise _refuse(f"{here.relative_to(root)} is a symbolic link")
    for current, directories, files in os.walk(root / relative):
        for name in [*directories, *files]:
            entry = Path(current) / name
            if entry.is_symlink():
                raise _refuse(f"{entry.relative_to(root)} is a symbolic link")


def read(root: Path, relative: PurePosixPath) -> InRepo:
    """The in-repo extension at `relative`, checked against every rule."""
    directory = root / relative
    _check_links(root, relative)
    if not directory.is_dir():
        raise _refuse(f"{relative} does not exist")
    metadata = directory / "pyproject.toml"
    try:
        raw = metadata.read_bytes()
        doc = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise Failure(ExitCode.CONFIG, f"{relative}/pyproject.toml: {exc}") from None
    project = doc.get("project") if isinstance(doc.get("project"), dict) else {}
    build = doc.get("build-system") if isinstance(doc.get("build-system"), dict) else {}
    name, version = project.get("name"), project.get("version")
    if "version" in project.get("dynamic", []) or not isinstance(version, str):
        raise Failure(
            ExitCode.CONFIG, f"{relative}/pyproject.toml: [project] needs a static version"
        )
    if not isinstance(name, str) or not name:
        raise Failure(ExitCode.CONFIG, f"{relative}/pyproject.toml: [project] needs a static name")
    requires = build.get("requires")
    if not build.get("build-backend") or not isinstance(requires, list):
        raise Failure(
            ExitCode.CONFIG,
            f"{relative}/pyproject.toml: declare [build-system] with requires and build-backend",
        )
    normalized = normalize_name(name)
    if relative.parts[1] != normalized:
        raise _refuse(
            f"{relative} holds the distribution {name}; its directory must be "
            f"extensions/{normalized}"
        )
    entry_points = project.get("entry-points", {})
    return InRepo(
        name=normalized,
        version=version,
        path=str(relative),
        extras=tuple(sorted(project.get("optional-dependencies", {}))),
        groups=tuple(sorted(g for g in entry_points if g.startswith("vecten."))),
        build_requires=tuple(requires),
        metadata_sha256=hashlib.sha256(raw).hexdigest(),
    )


def check(root: Path, arg: str) -> InRepo:
    """The in-repo extension `arg` names, after every rule `extension add` applies."""
    return read(root, _relative(root, arg.rstrip("/")))
