#!/usr/bin/env python3
"""Prepare a sandbox project for a release candidate and check it before it goes public."""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

PACKAGES = ("vecten", "vecten-shared", "vecten-platform", "vecten-mcp")
FIND_LINKS = " ".join(f"--find-links https://test.pypi.org/simple/{p}/" for p in PACKAGES)
ACCESS = Path(__file__).resolve().with_name("access.py")
SECRET_FILE_NAMES = (".env", ".env.*", "*.pem", "*.key")
REFUSED = 3
VERSION_VARIABLES = ("$VECTEN_VERSION", "${VECTEN_VERSION}")

PIN = re.compile(r'vecten==([^"\s]+)')
INSTALL = re.compile(rf'(uv tool install "vecten==[^"\s]+")(?: {re.escape(FIND_LINKS)})?')
UVX = re.compile(rf'uvx (?:{re.escape(FIND_LINKS)} )?(--with "vecten==[^"\s]+")')
INSTALL_PATCHED = re.compile(rf'(uv tool install "vecten==[^"\s]+") {re.escape(FIND_LINKS)}')
UVX_PATCHED = re.compile(rf'uvx {re.escape(FIND_LINKS)} (--with "vecten==[^"\s]+")')


class Refused(Exception):
    pass


def patch(text: str, candidate: str) -> str:
    for version in PIN.findall(text):
        if version not in VERSION_VARIABLES and version != candidate:
            raise Refused(f"pins vecten=={version}, not the candidate {candidate}")
    text = INSTALL.sub(lambda m: f"{m.group(1)} {FIND_LINKS}", text)
    text = UVX.sub(lambda m: f"uvx {FIND_LINKS} {m.group(1)}", text)
    # A pin written in a form the patterns miss would install from the production index.
    for number, line in enumerate(text.splitlines(), 1):
        patched = len(INSTALL_PATCHED.findall(line)) + len(UVX_PATCHED.findall(line))
        if patched != line.count("vecten=="):
            raise Refused(
                f"line {number} pins vecten in a form that cannot be patched: {line.strip()}"
            )
    return text


def unpatch(text: str) -> str:
    text = INSTALL_PATCHED.sub(r"\1", text)
    return UVX_PATCHED.sub(r"uvx \1", text)


def workflow_files(project: Path) -> list[Path]:
    directory = project / ".github" / "workflows"
    files = sorted(p for p in directory.glob("*") if p.is_file()) if directory.is_dir() else []
    if not files:
        raise Refused(f"no workflows under {directory}")
    return files


def rewrite(project: Path, convert) -> None:
    # Bytes in and out keep line endings; every file converts before any is written.
    changed = {}
    for path in workflow_files(project):
        try:
            changed[path] = convert(path.read_bytes().decode()).encode()
        except Refused as refused:
            raise Refused(f"{path.relative_to(project)}: {refused}") from None
    for path, data in changed.items():
        path.write_bytes(data)


def tracked_files(project: Path) -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=project, capture_output=True)
    if listed.returncode != 0:
        raise Refused(f"{project} is not a git work tree")
    names = [name for name in listed.stdout.decode().split("\0") if name]
    if not names:
        raise Refused(f"the git index of {project} is empty; add the files to push first")
    return names


def secret_names(names: Sequence[str]) -> list[str]:
    # The patterns are lower case, and a secret file's name may be written in any case.
    return [
        name
        for name in names
        if any(fnmatch.fnmatchcase(PurePosixPath(name).name.lower(), p) for p in SECRET_FILE_NAMES)
    ]


def secret_values(project: Path, tracked: Sequence[str]) -> list[str]:
    try:
        checked = subprocess.run(
            [sys.executable, str(ACCESS), "secrets-check", str(project)],
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise Refused(f"the secret check could not run: {error}") from None
    if checked.returncode == 0:
        if checked.stdout.strip():
            raise Refused("the secret check exited 0 but printed output")
        return []
    if checked.returncode == 1:
        paths = [line for line in checked.stdout.splitlines() if line]
        if not paths:
            last = (checked.stderr.strip().splitlines() or [""])[-1]
            raise Refused(f"the secret check failed without naming a path: {last}")
        if not set(paths) <= set(tracked):
            raise Refused("the secret check named a path that is not in the git index")
        return paths
    message = checked.stderr.strip() or checked.stdout.strip() or f"exit {checked.returncode}"
    raise Refused(f"the secret check could not run: {message}")


def check_public(project: Path) -> int:
    names = tracked_files(project)
    hits = [f"{name}  (file name)" for name in secret_names(names)]
    try:
        hits += [f"{name}  (credential value)" for name in secret_values(project, names)]
    except Refused:
        for hit in hits:
            print(hit)
        raise
    for hit in hits:
        print(hit)
    return 1 if hits else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    candidate = commands.add_parser("candidate", help="point the workflows at TestPyPI")
    candidate.add_argument("project", type=Path)
    candidate.add_argument("version")
    release = commands.add_parser("release", help="undo candidate, before vecten update")
    release.add_argument("project", type=Path)
    check = commands.add_parser("check-public", help="refuse a project holding a secret")
    check.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "candidate":
            rewrite(args.project, lambda text: patch(text, args.version))
        elif args.command == "release":
            rewrite(args.project, unpatch)
        else:
            return check_public(args.project)
    except Refused as error:
        print(f"sandbox_project: {error}", file=sys.stderr)
        return REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
