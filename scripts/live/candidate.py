#!/usr/bin/env python3
"""Cut a release candidate: branch rc/<version>rcN plus the annotated tag v<version>rcN."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence

BUMP = ["uv", "run", "vecten", "dev", "bump-version"]
LOCK = ["uv", "sync", "--locked", "--all-packages"]
VERSION = re.compile(r"\d+\.\d+\.\d+")
REFUSED = 3
WRAPPED = "<tools copy>/scripts/live/live candidate cut …"


class Refused(Exception):
    pass


def next_candidate(tags: Iterable[str], branches: Iterable[str], version: str) -> str:
    # A branch pushed without its tag has spent its number as surely as a tag has.
    tag = re.compile(rf"v{re.escape(version)}rc(\d+)")
    branch = re.compile(rf"origin/rc/{re.escape(version)}rc(\d+)")
    found = [int(m.group(1)) for name in tags if (m := tag.fullmatch(name))]
    found += [int(m.group(1)) for name in branches if (m := branch.fullmatch(name))]
    return f"{version}rc{max(found, default=0) + 1}"


def run(command: Sequence[str], what: str) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as error:
        raise Refused(f"{what} failed: {error}") from None
    if result.returncode != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise Refused(f"{what} failed: {lines[-1] if lines else f'exit {result.returncode}'}")
    return result.stdout.strip()


def git_ok(*args: str) -> bool:
    try:
        return subprocess.run(["git", *args], capture_output=True).returncode == 0
    except OSError:
        return False


def start_point() -> list[str]:
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    return [branch] if branch else ["--detach", run(["git", "rev-parse", "HEAD"], "git rev-parse")]


def abandon(origin: list[str], branch: str, tag: str) -> None:
    git_ok("merge", "--abort")
    git_ok("reset", "--hard", "HEAD")
    git_ok("clean", "-fd")
    git_ok("switch", *origin)
    git_ok("tag", "--delete", tag)
    git_ok("branch", "--delete", "--force", branch)


def cut(
    version: str, base: str, extra: Sequence[str], dry_run: bool, fetch: bool = True
) -> dict[str, object]:
    if not VERSION.fullmatch(version):
        raise Refused(f"version must be X.Y.Z, got {version!r}")
    if run(["git", "status", "--porcelain"], "git status"):
        raise Refused("the working tree is not clean")
    if fetch:
        run(["git", "fetch", "origin", "--tags"], "git fetch")
    tags = run(["git", "tag", "--list"], "git tag").split()
    remote = ["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/rc/"]
    candidate = next_candidate(tags, run(remote, "git for-each-ref").split(), version)
    branch, tag = f"rc/{candidate}", f"v{candidate}"
    if git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
        raise Refused(f"branch {branch} already exists")
    origin = start_point()
    created = False
    pushed = False
    try:
        run(["git", "switch", "--create", branch, base], f"creating {branch} from {base}")
        created = True
        for ref in extra:
            run(["git", "merge", "--no-ff", "--no-edit", ref], f"merging {ref}")
        run([*BUMP, candidate], "bump-version")
        run(LOCK, "uv sync --locked")
        run(["git", "add", "--update"], "git add")
        run(["git", "commit", "--message", f"chore(release): {candidate}"], "git commit")
        run(["git", "tag", "--annotate", "--message", f"Release candidate {candidate}", tag], "tag")
        commit = run(["git", "rev-parse", "HEAD"], "git rev-parse")
        if not dry_run:
            run(["git", "push", "origin", branch], f"pushing {branch}")
            pushed = True
            run(["git", "push", "origin", tag], f"pushing {tag}")
    except BaseException as error:
        if created:
            abandon(origin, branch, tag)
        if pushed and isinstance(error, Refused):
            raise Refused(f"{error}; branch {branch} is already on origin") from None
        raise
    return {
        "candidate": candidate,
        "branch": branch,
        "tag": tag,
        "commit": commit,
        "pushed": pushed,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cut_parser = commands.add_parser("cut", help="cut the next candidate and push it")
    cut_parser.add_argument("--version", required=True, help="the release, X.Y.Z")
    cut_parser.add_argument("--base", help="default: origin/release/<version>")
    cut_parser.add_argument("--with", dest="extra", action="append", default=[], metavar="REF")
    cut_parser.add_argument("--dry-run", action="store_true", help="skip the two pushes")
    args = parser.parse_args(argv)
    # Outside the wrapper git would fetch and push with the maintainer's own credentials.
    wrapped = os.environ.get("VECTEN_LIVE") == "1"
    if not wrapped and not args.dry_run:
        print(
            f"candidate: cut pushes with the test credentials only; run it as `{WRAPPED}`",
            file=sys.stderr,
        )
        return REFUSED
    try:
        base = args.base or f"origin/release/{args.version}"
        if not wrapped:
            print(
                "candidate: dry run outside the wrapper: origin was not fetched, so the number"
                " follows the tags and branches this clone already has",
                file=sys.stderr,
            )
        print(json.dumps(cut(args.version, base, args.extra, args.dry_run, fetch=wrapped)))
    except Refused as error:
        print(f"candidate: {error}", file=sys.stderr)
        return REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
