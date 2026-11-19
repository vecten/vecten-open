#!/usr/bin/env python3
"""Estimate a live run's cost against the ceiling, total what it spent, and sweep for leftovers."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

COMBOS = ("aws-postgres", "aws-snowflake", "scaleway-postgres", "scaleway-snowflake")
PROFILE = {"aws": "standard", "scaleway": "minimal"}
CEILING_USD = 60
HOURS_PER_MONTH = 730
REFUSED = 2
OVER_CEILING = 3
OUTSIDE_WRAPPER = 3
LISTING_FAILED = 4
UNKNOWN = "unknown"
TAG = "vecten:project"
ENV_TAG = "vecten:env"
TIMEOUT_SECONDS = 120
ESTIMATE = ["uv", "run", "vecten", "estimate"]

# The rendered Terraform root sets `vecten:project` and `vecten:env` as the AWS provider's
# default tags, so every AWS resource Terraform creates carries both. The tagging API answers
# for one region, so the listing runs once per enabled region.
AWS_REGIONS = ["aws", "ec2", "describe-regions", "--output", "json"]
AWS_TAGGED = [
    "aws",
    "resourcegroupstaggingapi",
    "get-resources",
    "--tag-filters",
    f"Key={TAG}",
    "--output",
    "json",
]
AWS_SECRETS = [
    "aws",
    "secretsmanager",
    "list-secrets",
    "--filters",
    "Key=name,Values=vecten/",
    "--output",
    "json",
]
AWS_ROLES = ["aws", "iam", "list-roles", "--output", "json"]
AWS_POLICIES = ["aws", "iam", "list-policies", "--scope", "Local", "--output", "json"]
AWS_BUCKETS = ["aws", "s3api", "list-buckets", "--output", "json"]
SCW_PROJECTS = ["scw", "account", "project", "list", "-o", "json"]
SCW_APPLICATIONS = ["scw", "iam", "application", "list", "-o", "json"]
SCW_KEYS = ["scw", "iam", "api-key", "list", "-o", "json"]
# `{project_id}` is filled per Scaleway project: a listing without it covers one project only.
SCW_INSTANCES = [
    "scw",
    "instance",
    "server",
    "list",
    "zone=all",
    "project-id={project_id}",
    "-o",
    "json",
]
SCW_DATABASES = [
    "scw",
    "rdb",
    "instance",
    "list",
    "region=all",
    "project-id={project_id}",
    "-o",
    "json",
]
# Bootstrap's own names: `-<suffix>` after a `vecten-<project>-<env>` prefix.
OWNED_SUFFIX = re.compile(r"-(deploy|plan|boundary|platform|mcp|main|state(-\d{12})?)")

# Mirrors names.name_prefix: `vecten-<project slug cut to 44 - len(env slug)>-<env slug>`.
PREFIX = "vecten-"
SLUG_BUDGET = 44


class Refused(Exception):
    pass


class ListingError(Exception):
    pass


class Found(NamedTuple):
    kind: str
    name: str
    project: str
    env: str | None = None


class Leftover(NamedTuple):
    cloud: str
    kind: str
    name: str
    project: str
    env: str | None = None


class PartialListing(Exception):
    """A listing that found `found` but failed for the parts named in `failures`."""

    def __init__(self, found: list[Found], failures: list[str]) -> None:
        super().__init__("; ".join(failures))
        self.found = found
        self.failures = failures


class Environment(NamedTuple):
    combination: str
    project: str
    env: str
    created_at: datetime
    destroyed_at: datetime | None

    @classmethod
    def parse(cls, raw: dict) -> Environment:
        try:
            combination = str(raw["combination"])
            if combination not in COMBOS:
                raise Refused(f"the created file names unknown combination {combination!r}")
            destroyed = raw.get("destroyed_at")
            return cls(
                combination,
                str(raw["project"]),
                str(raw["env"]),
                _moment(raw["created_at"]),
                _moment(destroyed) if destroyed else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise Refused(f"an entry of the created file is malformed: {error!r}") from None


class SweepResult(NamedTuple):
    leftovers: list[Leftover]
    failed: list[str]


Lister = Callable[[], list[Found]]


def _moment(value: object) -> datetime:
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        raise ValueError(f"{value!r} has no timezone")
    return moment


def _slug(value: str) -> str:
    out = "".join(c if c.isascii() and (c.islower() or c.isdigit()) else "-" for c in value.lower())
    return "-".join(part for part in out.split("-") if part)


def name_prefix(project: str, env: str) -> str:
    env_slug = _slug(env)
    part = _slug(project)[: SLUG_BUDGET - len(env_slug)].rstrip("-")
    return f"{PREFIX}{part}-{env_slug}"


def owner_of(environments: Sequence[Environment]) -> Callable[[str], tuple[str, str | None]]:
    """The (project, env) whose name prefix, then one of bootstrap's own suffixes, makes `name`.

    A name that is only a prefix match, such as `vecten-acme-prod-x-plan` for project `acme`
    and env `prod`, may belong to another environment, so it stays unclaimed.
    """
    prefixes = sorted(
        ((name_prefix(e.project, e.env), e.project, e.env) for e in environments),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    def owner(name: str) -> tuple[str, str | None]:
        for prefix, project, env in prefixes:
            rest = name.removeprefix(prefix)
            if name.startswith(prefix) and (rest == "" or OWNED_SUFFIX.fullmatch(rest)):
                return project, env
        return UNKNOWN, None

    return owner


def estimate(
    combinations: Sequence[str], hours: float, monthly: Callable[[str, str, str], float]
) -> float:
    return sum(_cost(c, hours, monthly) for c in combinations)


def _cost(combination: str, hours: float, monthly: Callable[[str, str, str], float]) -> float:
    cloud, warehouse = combination.split("-")
    price = monthly(cloud, PROFILE[cloud], warehouse)
    if not math.isfinite(price) or price < 0:
        raise Refused(f"the monthly price of {combination} is {price}, not a usable number")
    return price / HOURS_PER_MONTH * hours


def vecten_monthly(cloud: str, profile: str, warehouse: str) -> float:
    cmd = [*ESTIMATE, "--cloud", cloud, "--profile", profile, "--warehouse", warehouse, "--json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise Refused(f"vecten estimate did not finish in {TIMEOUT_SECONDS} seconds") from None
    except OSError as error:
        raise Refused(f"vecten estimate did not run: {error}") from None
    if result.returncode != 0:
        raise Refused(f"vecten estimate failed for {cloud}-{warehouse}: {_last(result.stderr)}")
    try:
        return float(json.loads(result.stdout)["clouds"][cloud]["monthly_usd"])
    except ValueError, KeyError, TypeError:
        raise Refused(f"vecten estimate printed no monthly price for {cloud}") from None


def _last(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def _first(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else "no message"


def run_json(cmd: Sequence[str]) -> object:
    try:
        result = subprocess.run(list(cmd), capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise ListingError(f"{cmd[0]} did not finish in {TIMEOUT_SECONDS} seconds") from None
    except OSError as error:
        raise ListingError(f"{cmd[0]} did not run: {error.strerror or error}") from None
    if result.returncode != 0:
        raise ListingError(f"{cmd[0]} exited {result.returncode}: {_last(result.stderr)}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise ListingError(f"{cmd[0]} printed output that is not JSON") from None


def _tag_value(tags: Sequence[str], wanted: str) -> str | None:
    for tag in tags:
        key, _, value = tag.partition("=")
        if key == wanted and value:
            return value
    return None


def real_listers(owner: Callable[[str], tuple[str, str | None]]) -> dict[tuple[str, str], Lister]:
    """Each listing keyed by (cloud, kind), in the order the sweep runs them.

    Resources the infrastructure code tags carry their project in the tag. What bootstrap
    makes outside Terraform is found by the names in deploy/names.py, deploy/aws/bootstrap.py
    and deploy/scaleway/bootstrap.py: the AWS state bucket, `vecten/<project>/<env>/` secrets,
    the deploy and plan roles and the boundary policy; the Scaleway projects (their description
    marker names the project), the IAM applications and their keys.
    """

    def aws_tagged() -> list[Found]:
        regions = [r["RegionName"] for r in run_json(AWS_REGIONS)["Regions"]]
        out: list[Found] = []
        failures = []
        for region in regions:
            try:
                items = run_json([*AWS_TAGGED, "--region", region])["ResourceTagMappingList"]
                for item in items:
                    tags = {t["Key"]: t["Value"] for t in item["Tags"]}
                    if TAG in tags:
                        out.append(
                            Found("tagged", item["ResourceARN"], tags[TAG], tags.get(ENV_TAG))
                        )
            except Exception as error:
                failures.append(f"region {region}: {_first(str(error))}")
        if failures:
            raise PartialListing(out, failures)
        return out

    def aws_secrets() -> list[Found]:
        names = [s["Name"] for s in run_json(AWS_SECRETS)["SecretList"]]
        return [Found("secret", n, *n.split("/")[1:3]) for n in names if n.count("/") >= 3]

    def aws_roles() -> list[Found]:
        names = [r["RoleName"] for r in run_json(AWS_ROLES)["Roles"]]
        wanted = [n for n in names if n.startswith(PREFIX) and n.endswith(("-deploy", "-plan"))]
        return [Found("role", n, *owner(n)) for n in wanted]

    def aws_policies() -> list[Found]:
        names = [p["PolicyName"] for p in run_json(AWS_POLICIES)["Policies"]]
        wanted = [n for n in names if n.startswith(PREFIX) and n.endswith("-boundary")]
        return [Found("boundary-policy", n, *owner(n)) for n in wanted]

    def aws_buckets() -> list[Found]:
        names = [b["Name"] for b in run_json(AWS_BUCKETS)["Buckets"]]
        wanted = [n for n in names if n.startswith(PREFIX) and "-state-" in n]
        return [Found("state-bucket", n, *owner(n)) for n in wanted]

    def marker_projects() -> list[dict[str, str]]:
        out = []
        for item in run_json(SCW_PROJECTS):
            parts = str(item.get("description") or "").split(":", 2)
            if len(parts) == 3 and parts[0] == "vecten" and parts[1] in ("resources", "state"):
                project, _, env = parts[2].rpartition(":")
                label = f"{item['name']} ({item['id']})"
                out.append({"id": item["id"], "label": label, "project": project, "env": env})
        return out

    projects_once: list = []

    def scw_marker_projects() -> list[dict[str, str]]:
        if not projects_once:
            try:
                projects_once.append(marker_projects())
            except Exception as error:
                projects_once.append(error)
        if isinstance(projects_once[0], Exception):
            raise projects_once[0]
        return projects_once[0]

    def scw_projects() -> list[Found]:
        return [Found("project", p["label"], p["project"], p["env"]) for p in scw_marker_projects()]

    def scw_applications() -> list[Found]:
        apps = run_json(SCW_APPLICATIONS)
        return [
            Found("application", f"{a['name']} ({a['id']})", *owner(a["name"]))
            for a in apps
            if a["name"].startswith(PREFIX)
        ]

    def scw_keys() -> list[Found]:
        names = {a["id"]: a["name"] for a in run_json(SCW_APPLICATIONS)}
        out = []
        for key in run_json(SCW_KEYS):
            name = names.get(key.get("application_id"), "")
            if name.startswith(PREFIX):
                out.append(Found("api-key", key["access_key"], *owner(name)))
        return out

    def scw_tagged(template: list[str], kind: str) -> list[Found]:
        try:
            projects = scw_marker_projects()
        except Exception as error:
            raise ListingError(
                f"cannot scope to the vecten projects: {_first(str(error))}"
            ) from None
        out: list[Found] = []
        failures = []
        for project in projects:
            cmd = [part.format(project_id=project["id"]) for part in template]
            try:
                for item in run_json(cmd):
                    tags = item.get("tags") or []
                    tagged = _tag_value(tags, TAG)
                    if tagged:
                        name = f"{item['name']} ({item['id']})"
                        out.append(Found(kind, name, tagged, _tag_value(tags, ENV_TAG)))
            except Exception as error:
                failures.append(f"project {project['label']}: {_first(str(error))}")
        if failures:
            raise PartialListing(out, failures)
        return out

    return {
        ("aws", "tagged"): aws_tagged,
        ("aws", "secret"): aws_secrets,
        ("aws", "role"): aws_roles,
        ("aws", "boundary-policy"): aws_policies,
        ("aws", "state-bucket"): aws_buckets,
        ("scaleway", "instance"): lambda: scw_tagged(SCW_INSTANCES, "instance"),
        ("scaleway", "database"): lambda: scw_tagged(SCW_DATABASES, "database"),
        ("scaleway", "project"): scw_projects,
        ("scaleway", "application"): scw_applications,
        ("scaleway", "api-key"): scw_keys,
    }


def sweep(
    environments: Sequence[Environment], listers: dict[tuple[str, str], Lister]
) -> SweepResult:
    live = [e for e in environments if e.destroyed_at is None]
    live_pairs = {(e.project, e.env) for e in live}
    live_projects = {e.project for e in live}

    def accounted(found: Found) -> bool:
        if found.env is None:
            return found.project in live_projects
        return (found.project, found.env) in live_pairs

    leftovers: list[Leftover] = []
    failed: list[str] = []
    for (cloud, kind), lister in listers.items():
        try:
            found = lister()
        except PartialListing as partial:
            found = partial.found
            failed += [f"could not list {cloud} {kind}: {detail}" for detail in partial.failures]
        except Exception as error:  # a listing that fails for any reason must not read as clean
            failed.append(f"could not list {cloud} {kind}: {_first(str(error))}")
            continue
        leftovers += [
            Leftover(cloud, kind, f.name, f.project, f.env) for f in found if not accounted(f)
        ]
    return SweepResult(leftovers, failed)


def load_created(path: Path) -> list[Environment]:
    try:
        document = json.loads(path.read_text())
        raw = document["environments"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Refused(f"cannot read the created file {path}: {error}") from None
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise Refused(f"the created file {path} has no list of environments")
    return [Environment.parse(item) for item in raw]


def _dollars(value: float) -> str:
    return f"${value:,.2f}"


def _report(per_combination: dict[str, float], as_json: bool, over: bool | None = None) -> None:
    total = sum(per_combination.values())
    if as_json:
        document = {
            "combinations": {c: round(v, 2) for c, v in per_combination.items()},
            "total_usd": round(total, 2),
        }
        if over is not None:
            document |= {"ceiling_usd": CEILING_USD, "over_ceiling": over}
        print(json.dumps(document))
        return
    for combination, value in per_combination.items():
        print(f"{combination:<20} {_dollars(value)}")
    print(f"{'total':<20} {_dollars(total)}")


def run_estimate(
    combinations: str, hours: float, as_json: bool, monthly: Callable[[str, str, str], float]
) -> int:
    names = [c for c in combinations.split(",") if c]
    unknown = [c for c in names if c not in COMBOS]
    if not names or unknown:
        raise Refused(
            f"unknown combinations {unknown or combinations!r}; known: {', '.join(COMBOS)}"
        )
    if not math.isfinite(hours) or hours <= 0:
        raise Refused("--hours must be a number above zero")
    return _over_ceiling({c: _cost(c, hours, monthly) for c in names}, as_json)


def _over_ceiling(per: dict[str, float], as_json: bool) -> int:
    over = round(sum(per.values()), 2) > CEILING_USD
    _report(per, as_json, over)
    if over:
        print(f"cost: over the ${CEILING_USD} ceiling: ask the maintainer", file=sys.stderr)
        return OVER_CEILING
    return 0


def run_spent(
    path: Path, as_json: bool, monthly: Callable[[str, str, str], float], now: datetime
) -> int:
    per: dict[str, float] = {}
    for env in load_created(path):
        hours = ((env.destroyed_at or now) - env.created_at).total_seconds() / 3600
        per[env.combination] = per.get(env.combination, 0.0) + _cost(
            env.combination, max(hours, 0.0), monthly
        )
    return _over_ceiling({c: per[c] for c in COMBOS if c in per}, as_json)


def run_sweep(path: Path, as_json: bool, listers: dict[tuple[str, str], Lister] | None) -> int:
    environments = load_created(path)
    result = sweep(environments, listers or real_listers(owner_of(environments)))
    if as_json:
        print(
            json.dumps(
                {
                    "leftovers": [leftover._asdict() for leftover in result.leftovers],
                    "failed": result.failed,
                }
            )
        )
    else:
        for line in result.failed:
            print(line)
        for item in result.leftovers:
            where = f"project {item.project}" + (f", env {item.env}" if item.env else "")
            print(f"leftover: {item.cloud} {item.kind} {item.name} ({where})")
        if not result.failed and not result.leftovers:
            print("nothing left")
    if result.failed:
        return LISTING_FAILED
    return 1 if result.leftovers else 0


def main(
    argv: Sequence[str] | None = None,
    *,
    monthly: Callable[[str, str, str], float] | None = None,
    listers: dict[tuple[str, str], Lister] | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    est = commands.add_parser("estimate", help="price a run against the ceiling")
    est.add_argument("--combinations", required=True, help="comma-separated combinations")
    est.add_argument("--hours", type=float, required=True)
    spent = commands.add_parser("spent", help="price the environments of the created file")
    sweeper = commands.add_parser("sweep", help="list what no live environment accounts for")
    for sub in (spent, sweeper):
        sub.add_argument("--created", type=Path, required=True)
    for sub in (est, spent, sweeper):
        sub.add_argument("--json", action="store_true", help="print one JSON document only")
    args = parser.parse_args(argv)
    price = monthly or vecten_monthly
    # Outside the wrapper the cloud CLIs would list the maintainer's own accounts.
    if args.command == "sweep" and os.environ.get("VECTEN_LIVE") != "1":
        print(
            "cost: sweep lists the test accounts only; run it as `uv run python"
            " scripts/live/access.py run -- uv run python scripts/live/cost.py sweep …`",
            file=sys.stderr,
        )
        return OUTSIDE_WRAPPER
    try:
        if args.command == "estimate":
            return run_estimate(args.combinations, args.hours, args.json, price)
        if args.command == "spent":
            return run_spent(args.created, args.json, price, now or datetime.now(UTC))
        return run_sweep(args.created, args.json, listers)
    except Refused as error:
        print(f"cost: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    sys.exit(main())
