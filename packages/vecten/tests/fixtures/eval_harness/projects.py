"""The starting project of each evaluation case that begins after a deploy.

`up` builds it from the code the scenario tests use, so the agent starts where a person would:
the fixture project rendered with its deploy layer, committed, deployed to the mocked cloud and
pushed to the gh stand-in, whose post-receive hook ships it as `ship.yml` would. A case's
project is built once; `clean` removes it.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from eval_harness import gh_standin

REPOSITORY = "acme/data"
PACKAGE_TESTS = Path(__file__).resolve().parents[2]
SEMANTIC_GRADER = PACKAGE_TESTS.parent / "src/vecten/knowhow/skills/semantic/evals/grader"
TESTS_ALIAS = "eval_harness_tests"
AMOUNT_COLUMN = "      - name: amount\n        description: Deal value in the source currency.\n"
NOT_NULL = "        data_tests: [not_null]\n"


# The rest scaffold `vecten connector add demo --from rest` renders, filled in for the demo
# API as the `connector` skill prescribes, so the model case starts where that skill ends.
DEMO_CONNECTOR = '''"""demo: the demo CRM API's companies and deals, read incrementally."""

from dlt.sources.rest_api import rest_api_source

from vecten_platform.sync import secret

UPDATED_SINCE = {
    "type": "incremental",
    "cursor_path": "updated_at",
    "initial_value": "1970-01-01T00:00:00Z",
}


def source():
    config = {
        "client": {
            "base_url": "https://api.demo.test/v1/",
            "auth": {"type": "bearer", "token": secret("DEMO_API_KEY")},
            "paginator": {
                "type": "cursor",
                "cursor_path": "next_cursor",
                "cursor_param": "cursor",
            },
        },
        "resource_defaults": {"write_disposition": "merge", "primary_key": "id"},
        "resources": [
            {
                "name": name,
                "endpoint": {
                    "path": path,
                    "data_selector": "data",
                    "params": {"limit": 200, "updated_since": UPDATED_SINCE},
                },
            }
            for name, path in (("company", "companies"), ("deal", "deals"))
        ],
    }
    return rest_api_source(config, name="demo")
'''


def schema_drift(actual: dict, committed: dict) -> list[str]:
    """Each difference between the `vecten raw schema` a run printed and the committed one."""

    def columns(doc):
        return {
            (table["name"], column["name"]): column["type"]
            for table in doc["tables"]
            for column in table["columns"]
        }

    landed, fixture = columns(actual), columns(committed)
    found = []
    for key in sorted(landed.keys() | fixture.keys()):
        name = ".".join(key)
        if key not in fixture:
            found.append(f"{name}: landed, missing from the fixture")
        elif key not in landed:
            found.append(f"{name}: in the fixture, not landed")
        elif landed[key] != fixture[key]:
            found.append(f"{name}: {fixture[key]} in the fixture, {landed[key]} landed")
    return found


@dataclass(frozen=True)
class Built:
    """A case's project, and what `up` serves beside it: the variables env.sh exports, the
    gaps the MCP starts with, and the existing warehouse's table."""

    root: Path
    environ: dict[str, str] = field(default_factory=dict)
    gaps: dict[str, dict] = field(default_factory=dict)
    warehouse: object | None = None


@dataclass(frozen=True)
class Builder:
    render: Callable[[Path, str], Path]
    prepare: Callable[[Path, Path], None]
    environ: Callable[[Path], dict[str, str]] = lambda case_dir: {}
    grader: str | None = None


def tests_module(name: str):
    """A module of this package's test suite, imported under a name of its own, so this
    process needs no `tests` package on its path."""
    if TESTS_ALIAS not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            TESTS_ALIAS,
            PACKAGE_TESTS / "__init__.py",
            submodule_search_locations=[str(PACKAGE_TESTS)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[TESTS_ALIAS] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{TESTS_ALIAS}.{name}")


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@contextmanager
def _environ(values: dict[str, str]) -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def seed_deployed_repo(
    case, case_dir: Path, builder: Builder, without_skill: str | None = None
) -> Path:
    """Render, deploy and push the project the case starts from; its root. `without_skill`
    names a skill the project never vendored, for a baseline run."""
    from vecten.stack.env import ensure_local_env
    from vecten_shared.config import load_config, save_config

    root = builder.render(case_dir, case.domain)
    builder.prepare(root, case_dir)
    if without_skill:
        drop_skill(root, without_skill)
    github, cloud = case_dir / "github", case_dir / "cloud"
    cloud.mkdir(parents=True, exist_ok=True)
    store = gh_standin.Store(github)
    if store.read(REPOSITORY) is None:
        store.create(REPOSITORY, private=True)
    pushing = {
        **os.environ,
        **gh_standin.git_rewrite(github, os.environ),
        **gh_standin.mock_cloud_environ(cloud, os.environ),
    }
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Acme Data")
    _git(root, "config", "user.email", "data@acme.test")
    first = _commit(root, "Initial project")
    _git(root, "remote", "add", "origin", f"https://github.com/{REPOSITORY}")
    _git(root, "push", "-q", "-u", "origin", "main", env=pushing)
    facts = load_config(root)
    prod = facts.environments["prod"]
    prod.bootstrapped, prod.state_bucket, prod.deployed_commit = True, "mock-state", first
    save_config(root, facts)
    if str(gh_standin.MOCK_CLOUD_DIR) not in sys.path:
        sys.path.insert(0, str(gh_standin.MOCK_CLOUD_DIR))
    with _environ(gh_standin.mock_cloud_environ(cloud, os.environ)):
        from vecten_mock_cloud import MockCloud

        plane = MockCloud(root, "prod")
        plane.store_outputs(gh_standin.deployed_outputs(facts, "prod"))
        for connector in facts.connectors:
            for name in connector.secrets:
                plane.put_secret(name, f"eval-seeded-{name.lower()}")
    _commit(root, "Record the first deploy")
    # Pushed after the deploy is recorded, so the stand-in ships it to the mocked cloud.
    _git(root, "push", "-q", "origin", "main", env=pushing)
    # The local stack's secrets as the person's first `vecten up` left them, in the git-ignored
    # .env, so the tree stays clean.
    ensure_local_env(root, load_config(root))
    return root


def drop_skill(root: Path, name: str) -> None:
    """The project as if `vecten init` had never vendored the skill: its folder, its entry in
    CLAUDE.md and its files in the update baseline go."""
    shutil.rmtree(root / "skills" / name)
    kept, entry = [], False
    for line in (root / "CLAUDE.md").read_text().splitlines(keepends=True):
        entry = line.startswith(f"- `skills/{name}/SKILL.md`") or (entry and line.startswith("  "))
        if not entry:
            kept.append(line)
    (root / "CLAUDE.md").write_text("".join(kept))
    baseline = root / ".copier-baseline.json"
    doc = json.loads(baseline.read_text())
    doc["files"] = {k: v for k, v in doc["files"].items() if not k.startswith(f"skills/{name}/")}
    baseline.write_text(json.dumps(doc, indent=2) + "\n")


def build(case, case_dir: Path, without_skill: str | None = None) -> Built | None:
    """The case's starting project, built on the first call and kept after it; None for a
    case that starts from an empty folder."""
    builder = BUILDERS.get(f"{case.skill}-{case.eval_id}")
    if builder is None:
        return None
    root = case_dir / "proj"
    if not (root / ".git").is_dir():
        root = seed_deployed_repo(case, case_dir, builder, without_skill)
    fixture = semantic_fixture(builder.grader) if builder.grader else {}
    gaps = {fixture["gap"]["id"]: gap_record(fixture["gap"])} if "gap" in fixture else {}
    warehouse = warehouse_seed(fixture) if "rows" in fixture else None
    return Built(root, builder.environ(case_dir), gaps, warehouse)


def semantic_fixture(scenario: str) -> dict:
    return json.loads((SEMANTIC_GRADER / f"{scenario}.json").read_text())


def gap_record(gap: dict) -> dict:
    """The gap as the feedback loop stores one after a person's report."""
    from vecten_mcp.tool_service import evidence

    now = time.time()
    return {
        "category": gap["category"],
        "reason": "",
        "datasets": gap["datasets"],
        "columns": [],
        "names": gap["names"],
        "occurrence_count": 1,
        "first_seen": now,
        "last_seen": now,
        "status": "open",
        "reporters": [],
        "evidence": evidence(gap["evidence"]),
    }


def warehouse_seed(fixture: dict):
    """The table the fixture's warehouse holds; the first field is its key."""
    from eval_harness.warehouse import Seed

    first, *rest = fixture["warehouse_fields"]
    sample = fixture["rows"][0]
    columns = [(first, "integer PRIMARY KEY")]
    for name in rest:
        kind = "text" if isinstance(sample[name], str) else "numeric(12, 2)"
        columns.append((name, f"{kind} NOT NULL"))
    rows = tuple(
        tuple(row[name] for name in fixture["warehouse_fields"]) for row in fixture["rows"]
    )
    return Seed(fixture["dataset"], tuple(columns), rows, f"One row per {fixture['primary_key']}.")


# --- the cases ----------------------------------------------------------------------------


def _render_hubspot(case_dir: Path, domain: str) -> Path:
    return tests_module("scenarios.hubspot").hubspot_project(case_dir, domain)


def _render_fixture(case_dir: Path, domain: str) -> Path:
    render = tests_module("fixture_project").render_fixture
    return render(case_dir, "postgres", deploy=True, domain=domain)


def _upstream_file(case_dir: Path) -> Path:
    return case_dir / "upstream" / "deals.json"


def _renamed_upstream(root: Path, case_dir: Path) -> None:
    """The upstream after HubSpot renamed `dealname`, which the connector still reads, and the
    fixtures the connector recorded before the rename."""
    hubspot = tests_module("scenarios.hubspot")
    path = _upstream_file(case_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    hubspot.upstream(path, "deal_name")
    fixtures = root / "connectors" / "hubspot" / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    rows = [{"id": id_, "dealname": name} for id_, name in hubspot.DEALS]
    (fixtures / "deals.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def _three_failures(root: Path, case_dir: Path) -> None:
    """operate-1: the renamed field; open deals the demo API now sends without an amount, which
    the mart's `not_null` test on `amount` rejects; and refunds that reach core while the
    `total_revenue` metric still counts them."""
    _renamed_upstream(root, case_dir)
    refunds = tests_module("scenarios.refunds")
    refunds.project_at_a(root)
    refunds.edit(root / "dbt/models/marts/schema.yml", AMOUNT_COLUMN, AMOUNT_COLUMN + NOT_NULL)
    deals = root / "connectors" / "demo" / "fixtures" / "deal.jsonl"
    rows = [json.loads(line) for line in deals.read_text().splitlines()]
    for row in rows:
        if row["stage"] == "open":
            row["amount"] = None
    deals.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _two_sources(root: Path, case_dir: Path) -> None:
    """operate-2: the HubSpot source whose key was revoked and the Stripe source that is down."""
    add_connector = tests_module("deploy_fixtures").add_connector
    add_connector(root, "hubspot", ("HUBSPOT_TOKEN",))
    add_connector(root, "stripe", ("STRIPE_KEY",))


def _upstream_environ(case_dir: Path) -> dict[str, str]:
    return {"HUBSPOT_FIXTURE_FILE": str(_upstream_file(case_dir))}


def _refunds_in_core(root: Path, case_dir: Path) -> None:
    """semantic-2: refunds reach core; the mart and the metric leave them out."""
    tests_module("scenarios.refunds").project_at_a(root)


def _refunds_in_the_mart(root: Path, case_dir: Path) -> None:
    """semantic-1: the mart and its dataset carry refunds; only the metric is wrong."""
    refunds = tests_module("scenarios.refunds")
    refunds.project_at_a(root)
    refunds.add_refunds_to_the_mart(root)


def _render_existing_warehouse(case_dir: Path, domain: str) -> Path:
    """An MCP-only project over the harness's existing warehouse, with its deploy layer."""
    from eval_harness.warehouse import ADMIN, DATABASE, HOST, PORT
    from vecten.scaffold.params import ScaffoldParams
    from vecten.scaffold.render import render_project

    root = case_dir / "proj"
    answers = {
        "name": "acme",
        "users": 3,
        "has_warehouse": "yes",
        "warehouse_url": f"postgresql://{ADMIN}@{HOST}:{PORT}/{DATABASE}",
        "cloud": "aws",
        "domain": domain,
        "dns_zone": domain,
        "github_repository": REPOSITORY,
    }
    render_project(root, ScaffoldParams.from_answers(answers, {}))
    return root


def _admin_ada(root: Path, case_dir: Path) -> None:
    """semantic-3: Ada administers the MCP, so she can sign in to read and edit its model."""
    from vecten_shared.config import load_config, save_config

    config = load_config(root)
    config.mcp.admins = ["ada@acme.test"]
    save_config(root, config)


BUILDERS: dict[str, Builder] = {
    "operate-1": Builder(_render_hubspot, _three_failures, _upstream_environ),
    "operate-2": Builder(_render_fixture, _two_sources),
    "operate-3": Builder(_render_hubspot, _renamed_upstream, _upstream_environ),
    "semantic-1": Builder(_render_fixture, _refunds_in_the_mart, grader="wrong-definition"),
    "semantic-2": Builder(_render_fixture, _refunds_in_core, grader="mart-column"),
    "semantic-3": Builder(_render_existing_warehouse, _admin_ada, grader="empty-model"),
}
