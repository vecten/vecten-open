#!/usr/bin/env python3
"""Prepare a sandbox project for a release candidate and check it before it goes public."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
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
# The wrapper's own secret check, which it runs before this script when it runs `check-public`.
SECRET_HITS = "VECTEN_LIVE_SECRET_HITS"
# The check must see at least what a push sends and `git add --all` would stage. A push sends
# the original objects, not what `refs/replace/` substitutes, and the push runs with another
# home, so only the project's own ignore files (.gitignore, .git/info/exclude, which `add`
# honours too) may hide a file; and reading the index would run any configured fsmonitor.
GIT = [
    "git",
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.excludesFile={os.devnull}",
]
# Where a push can publish a file from: `vecten github push` stages everything `git add --all`
# would, and a push sends every commit its refs reach. The same keys as the secret check's JSON.
PLACES = {"working-tree": "in the working tree", "staged": "staged", "commit": "in a commit"}
REWRITE = "removing the file is not enough: rewrite the history or render the project afresh"

PIN = re.compile(r'vecten==([^"\s]+)')
INSTALL = re.compile(rf'(uv tool install "vecten==[^"\s]+")(?: {re.escape(FIND_LINKS)})?')
RUNNER = r"(uvx|uv run --no-project)"
UV_WITH = re.compile(rf'{RUNNER} (?:{re.escape(FIND_LINKS)} )?(--with "vecten==[^"\s]+")')
INSTALL_PATCHED = re.compile(rf'(uv tool install "vecten==[^"\s]+") {re.escape(FIND_LINKS)}')
UV_WITH_PATCHED = re.compile(rf'{RUNNER} {re.escape(FIND_LINKS)} (--with "vecten==[^"\s]+")')


class Refused(Exception):
    pass


def patch(text: str, candidate: str) -> str:
    for version in PIN.findall(text):
        if version not in VERSION_VARIABLES and version != candidate:
            raise Refused(f"pins vecten=={version}, not the candidate {candidate}")
    text = INSTALL.sub(lambda m: f"{m.group(1)} {FIND_LINKS}", text)
    text = UV_WITH.sub(lambda m: f"{m.group(1)} {FIND_LINKS} {m.group(2)}", text)
    # A pin written in a form the patterns miss would install from the production index.
    for number, line in enumerate(text.splitlines(), 1):
        patched = len(INSTALL_PATCHED.findall(line)) + len(UV_WITH_PATCHED.findall(line))
        if patched != line.count("vecten=="):
            raise Refused(
                f"line {number} pins vecten in a form that cannot be patched: {line.strip()}"
            )
    return text


def unpatch(text: str) -> str:
    text = INSTALL_PATCHED.sub(r"\1", text)
    return UV_WITH_PATCHED.sub(r"\1 \2", text)


def workflow_files(project: Path) -> list[Path]:
    directory = project / ".github" / "workflows"
    root = project.resolve()
    files = []
    for path in sorted(directory.glob("*")) if directory.is_dir() else []:
        # A rewrite follows a link, so a link that leaves the project would change a file
        # elsewhere; one inside it names a workflow the loop meets on its own.
        if not path.resolve().is_relative_to(root):
            raise Refused(f"{path.relative_to(project)} leads outside the project")
        if path.is_file() and not path.is_symlink():
            files.append(path)
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


def _git(project: Path, *args: str, given: bytes = b"") -> bytes:
    try:
        done = subprocess.run([*GIT, *args], cwd=project, input=given, capture_output=True)
    except OSError as error:
        raise Refused(f"git could not start: {error}") from None
    if done.returncode != 0:
        lines = done.stderr.decode(errors="replace").strip().splitlines()
        last = lines[-1] if lines else f"exit {done.returncode}"
        raise Refused(f"cannot read what {project} would publish: git {args[0]} failed: {last}")
    return done.stdout


def _names(output: bytes) -> list[str]:
    return [name for name in output.decode(errors="surrogateescape").split("\0") if name]


def _has_head(project: Path) -> bool:
    """Whether HEAD names a commit; a repository with none yet answers exit 1 and nothing else."""
    try:
        done = subprocess.run(
            [*GIT, "rev-parse", "--verify", "--quiet", "HEAD"], cwd=project, capture_output=True
        )
    except OSError as error:
        raise Refused(f"git could not start: {error}") from None
    if done.returncode == 1 and not done.stdout.strip() and not done.stderr.strip():
        return False
    if done.returncode != 0:
        raise Refused(f"cannot read what {project} would publish: git rev-parse failed")
    return True


def published_paths(project: Path) -> dict[str, set[str]]:
    """Every path a push could publish, with where: the working tree as `git add --all` would
    stage it, the index, and every path a file had in any commit a ref or HEAD reaches."""
    listed = subprocess.run([*GIT, "ls-files", "-z", "--stage"], cwd=project, capture_output=True)
    if listed.returncode != 0:
        raise Refused(f"{project} is not a git work tree")
    # Listing from a subdirectory would cover only its part of the work tree and index.
    top = Path(_git(project, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != project.resolve():
        raise Refused(f"{project} is not the top level of its work tree, {top}")
    staged = [entry.partition("\t")[2] for entry in _names(listed.stdout)]
    if not staged:
        raise Refused(f"the git index of {project} is empty; add the files to push first")
    places: dict[str, set[str]] = {}
    staging = _git(project, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for name in _names(staging):
        if os.path.lexists(project / name):
            places.setdefault(name, set()).add("working-tree")
    for name in staged:
        places.setdefault(name, set()).add("staged")
    for name in history_paths(project):
        places.setdefault(name, set()).add("commit")
    return places


def history_paths(project: Path) -> set[str]:
    """Every path any file had in any commit a ref or HEAD reaches.

    Each commit is compared with each of its parents, and a root commit with nothing: a path in
    a commit's tree is either new or changed there against some parent, or the same in a
    parent, whose own comparisons are listed too, so walking back finds where it came in. One
    name per blob would miss a file renamed away from a forbidden name.
    """
    revisions = ["--all", *(["HEAD"] if _has_head(project) else [])]
    commits = _git(project, "rev-list", *revisions)
    changed = _git(
        project,
        "diff-tree",
        "--stdin",
        "--root",
        "-r",
        "-m",
        "--no-renames",
        "--no-commit-id",
        "--name-only",
        "-z",
        given=commits,
    )
    return set(_names(changed))


def secret_names(published: dict[str, set[str]]) -> list[dict]:
    # The patterns are lower case, and a secret file's name may be written in any case.
    return [
        {"path": name, "found": [place for place in PLACES if place in published[name]]}
        for name in sorted(published)
        if any(fnmatch.fnmatchcase(PurePosixPath(name).name.lower(), p) for p in SECRET_FILE_NAMES)
    ]


def _findings(text: str, published: dict[str, set[str]]) -> list[dict]:
    """The secret check's JSON findings, each a path a push could publish and where it was."""
    try:
        findings = json.loads(text)
    except ValueError:
        findings = None
    if not isinstance(findings, list) or not all(
        isinstance(finding, dict)
        and isinstance(finding.get("path"), str)
        and isinstance(finding.get("found"), list)
        and finding["found"]
        and set(finding["found"]) <= set(PLACES)
        for finding in findings
    ):
        raise Refused("the secret check's findings cannot be read")
    if not {finding["path"] for finding in findings} <= set(published):
        raise Refused("the secret check named a path a push of this project would not publish")
    return findings


def secret_values(project: Path, published: dict[str, set[str]]) -> list[dict]:
    handed = os.environ.get(SECRET_HITS)
    if handed is not None:
        return _findings(handed, published)
    try:
        checked = subprocess.run(
            [sys.executable, "-I", str(ACCESS), "secrets-check", "--json", str(project)],
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise Refused(f"the secret check could not run: {error}") from None
    if checked.returncode == 0:
        if checked.stdout.strip() != "[]":
            raise Refused("the secret check exited 0 but printed findings")
        return []
    if checked.returncode == 1:
        if not checked.stdout.strip():
            last = (checked.stderr.strip().splitlines() or [""])[-1]
            raise Refused(f"the secret check failed without naming a path: {last}")
        findings = _findings(checked.stdout, published)
        if not findings:
            raise Refused("the secret check exited 1 but named no path")
        return findings
    message = checked.stderr.strip() or checked.stdout.strip() or f"exit {checked.returncode}"
    raise Refused(f"the secret check could not run: {message}")


def describe(finding: dict, kind: str) -> str:
    """One line for a path that must not be published; never the value that was found."""
    places = finding["found"]
    note = f": {REWRITE}" if "commit" in places else ""
    return f"{finding['path']}  ({kind}; {', '.join(PLACES[p] for p in places)}{note})"


def check_public(project: Path) -> int:
    published = published_paths(project)
    hits = [describe(finding, "file name") for finding in secret_names(published)]
    try:
        values = secret_values(project, published)
        hits += [describe(finding, "credential value") for finding in values]
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
