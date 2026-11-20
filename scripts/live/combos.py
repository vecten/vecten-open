#!/usr/bin/env python3
"""Name the live combinations a release owes: those whose code changed since they last ran.

`affected` names those a fix between two candidates touches.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import NamedTuple

COMBOS = ("aws-postgres", "aws-snowflake", "scaleway-postgres", "scaleway-snowflake")
DEFAULT = {"aws": "aws-snowflake", "scaleway": "scaleway-postgres"}
SCOPES = (
    "none",
    "shared",
    "cloud:aws",
    "cloud:scaleway",
    "warehouse:snowflake",
    "warehouse:postgres",
)
HERE = Path(__file__).resolve().parent
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]*")
# How much of a ledger value that is no tag name a refusal repeats.
SHOWN_OF_VALUE = 20
REFUSED = 3
SHOWN = 5


class Refused(Exception):
    pass


class Rule(NamedTuple):
    scope: str
    paths: tuple[str, ...]


def load_rules(path: Path) -> list[Rule]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Refused(f"cannot read the file map {path}: {error}") from None
    rules = [Rule(str(r["scope"]), tuple(r["paths"])) for r in data.get("rule", [])]
    for rule in rules:
        if rule.scope not in SCOPES:
            raise Refused(f"file map {path}: unknown scope {rule.scope!r}")
    return rules


def classify(path: str, rules: list[Rule]) -> str:
    posix = PurePosixPath(path)
    for rule in rules:
        if any(posix.full_match(pattern) for pattern in rule.paths):
            return rule.scope
    return "shared"


def owed(
    ledger: dict[str, str], changed: Callable[[str], list[str]], rules: list[Rule]
) -> dict[str, list[str]]:
    specific: dict[str, list[str]] = {}
    shared: dict[str, list[str]] = {}
    seen: dict[str, list[str]] = {}
    for combo in COMBOS:
        tag = ledger[combo]
        if tag not in seen:
            seen[tag] = changed(tag)
        cloud, warehouse = combo.split("-")
        for path in seen[tag]:
            scope = classify(path, rules)
            if scope == "shared":
                shared.setdefault(combo, []).append(path)
            elif scope in (f"cloud:{cloud}", f"warehouse:{warehouse}"):
                specific.setdefault(combo, []).append(path)
    result = dict(specific)
    for cloud, default in DEFAULT.items():
        members = [combo for combo in COMBOS if combo.startswith(f"{cloud}-")]
        if any(combo in specific and combo in shared for combo in members):
            continue
        if default in shared:
            result.setdefault(default, []).extend(shared[default])
    return {combo: result[combo] for combo in COMBOS if combo in result}


def git_lines(*args: str) -> list[str]:
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True)
    except OSError as error:
        raise Refused(f"git failed: {error}") from None
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        raise Refused(
            f"git {args[0]} failed: {lines[-1] if lines else f'exit {result.returncode}'}"
        )
    return result.stdout.splitlines()


def has_tag(tag: str) -> bool:
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", f"refs/tags/{tag}"],
            capture_output=True,
        )
    except OSError as error:
        raise Refused(f"git failed: {error}") from None
    return probe.returncode == 0


def changed_between(earlier: str, later: str) -> list[str]:
    # The revisions come from files and arguments the agent writes: none may be read as an option.
    return git_lines(
        "diff", "--name-only", "--no-renames", "--end-of-options", earlier, later, "--"
    )


def git_changed(tag: str) -> list[str]:
    return changed_between(f"refs/tags/{tag}", "HEAD")


def touched(changed: list[str], rules: list[Rule]) -> dict[str, list[str]]:
    """The changed files that touch each combination, every shared file touching all four."""
    result: dict[str, list[str]] = {}
    for combo in COMBOS:
        cloud, warehouse = combo.split("-")
        reach = ("shared", f"cloud:{cloud}", f"warehouse:{warehouse}")
        files = [path for path in changed if classify(path, rules) in reach]
        if files:
            result[combo] = files
    return result


def affected(earlier: str, later: str, map_path: Path, as_json: bool) -> None:
    rules = load_rules(map_path)
    for tag in (earlier, later):
        if not TAG.fullmatch(tag) or not has_tag(tag):
            raise Refused(f"tag {tag!r} is not a tag this clone has")
    # Each candidate's own bump commit sits on top of the tree it tests and changes the version
    # files every candidate changes; the trees beneath the bumps differ only by the fixes.
    changed = changed_between(f"refs/tags/{earlier}^", f"refs/tags/{later}^")
    result = touched(changed, rules)
    untouched = [combo for combo in COMBOS if combo not in result]
    if as_json:
        print(json.dumps({"touched": result, "untouched": untouched}))
        return
    print(f"map {map_path.absolute()}")
    for combo, files in result.items():
        print(f"{combo:<20} {len(files)} files: {', '.join(files)}")
    print(f"untouched: {', '.join(untouched) if untouched else 'none'}")


def read_ledger(path: Path) -> dict[str, str]:
    try:
        ledger = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Refused(f"cannot read the ledger {path}: {error}") from None
    unknown = sorted(set(ledger) - set(COMBOS))
    if unknown:
        raise Refused(f"ledger names unknown combinations: {', '.join(unknown)}")
    missing = [combo for combo in COMBOS if not isinstance(ledger.get(combo), str)]
    if missing:
        raise Refused(f"ledger has no tag for: {', '.join(missing)}")
    for combo in COMBOS:
        if not TAG.fullmatch(ledger[combo]):
            shown = ledger[combo][:SHOWN_OF_VALUE]
            raise Refused(f"the ledger's {combo} is no tag name: it starts {shown!r}")
    return ledger


def plan(ledger_path: Path, map_path: Path, as_json: bool) -> None:
    ledger = read_ledger(ledger_path)
    rules = load_rules(map_path)
    for tag in sorted(set(ledger.values())):
        if not has_tag(tag):
            raise Refused(f"the ledger names tag {tag}, which this clone does not have")
    result = owed(ledger, git_changed, rules)
    not_owed = [combo for combo in COMBOS if combo not in result]
    if as_json:
        owed_json = {c: {"since": ledger[c], "files": files} for c, files in result.items()}
        print(json.dumps({"owed": owed_json, "not_owed": not_owed}))
        return
    print(f"ledger {ledger_path.absolute()}, map {map_path.absolute()}")
    for combo, files in result.items():
        listed = ", ".join(files[:SHOWN]) + (", ..." if len(files) > SHOWN else "")
        print(f"{combo:<20} since {ledger[combo]:<12} {len(files)} files: {listed}")
    print(f"not owed: {', '.join(not_owed) if not_owed else 'none'}")


def record(ledger_path: Path, combination: str, tag: str) -> None:
    if combination not in COMBOS:
        raise Refused(f"unknown combination {combination!r}; known: {', '.join(COMBOS)}")
    if not TAG.fullmatch(tag) or not has_tag(tag):
        raise Refused(f"tag {tag!r} is not a tag this clone has")
    try:
        text = ledger_path.read_text()
    except OSError as error:
        raise Refused(f"cannot read the ledger {ledger_path}: {error}") from None
    line = re.compile(rf"^{re.escape(combination)}\s*=.*$", re.MULTILINE)
    if not line.search(text):
        raise Refused(f"the ledger has no line for {combination}")
    ledger_path.write_text(line.sub(f'{combination} = "{tag}"', text, count=1))
    print(f"ledger {ledger_path.absolute()}")
    print(f"{combination} = {tag}")


def default_file(name: str, written: bool = False) -> Path:
    """The checkout the command runs in holds the ledger it records, not the clone this script
    runs from; the files beside the script stand in for reading when the directory has none."""
    local = Path.cwd() / "scripts" / "live" / name
    if local.is_file():
        return local
    if written:
        raise Refused(f"no scripts/live/{name} here; run it in the checkout that keeps the ledger")
    return HERE / name


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    paths = argparse.ArgumentParser(add_help=False)
    paths.add_argument("--ledger", type=Path, help="default: scripts/live/ledger.toml here")
    paths.add_argument("--map", type=Path, help="default: scripts/live/filemap.toml here")
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", parents=[paths], help="list the owed combinations")
    plan_parser.add_argument("--json", action="store_true", help="print JSON only")
    record_parser = commands.add_parser("record", parents=[paths], help="set a ledger tag")
    record_parser.add_argument("combination")
    record_parser.add_argument("tag")
    affected_parser = commands.add_parser(
        "affected", parents=[paths], help="the combinations a fix between two candidates touches"
    )
    affected_parser.add_argument("earlier", help="the earlier candidate's tag")
    affected_parser.add_argument("later", help="the later candidate's tag")
    affected_parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(argv)
    try:
        args.ledger = args.ledger or default_file("ledger.toml", args.command == "record")
        args.map = args.map or default_file("filemap.toml")
        if args.command == "plan":
            plan(args.ledger, args.map, args.json)
        elif args.command == "affected":
            affected(args.earlier, args.later, args.map, args.json)
        else:
            record(args.ledger, args.combination, args.tag)
    except Refused as error:
        print(f"combos: {error}", file=sys.stderr)
        return REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
