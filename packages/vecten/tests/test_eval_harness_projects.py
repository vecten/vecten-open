"""The starting projects the evaluation harness builds for the cases that begin after a
deploy: rendered from the fixture project, committed, deployed to the mocked cloud and pushed
to the gh stand-in, so the agent starts where a person would."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import gh_standin, issues_replay, projects  # noqa: E402
from eval_harness.cases import CASES  # noqa: E402

from vecten_shared.config import load_config  # noqa: E402

# Tests that share a module fixture's builds stay on one xdist worker and one CI shard, which
# builds the projects once; split up, every worker and shard would build all of them.
OPERATE_PROJECTS = pytest.mark.xdist_group("eval-harness-operate-projects")
SEMANTIC_PROJECTS = pytest.mark.xdist_group("eval-harness-semantic-projects")


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Each operate case's project, built once for the module."""
    base = tmp_path_factory.mktemp("eval")
    return {
        name: (base / name, projects.build(CASES[name], base / name))
        for name in ("operate-1", "operate-2", "operate-3")
    }


@OPERATE_PROJECTS
@pytest.mark.parametrize("name", ["operate-1", "operate-2", "operate-3"])
def test_an_operate_project_is_deployed_committed_and_pushed(built, name):
    case_dir, project = built[name]
    root = project.root
    facts = load_config(root).environments["prod"]
    assert facts.bootstrapped and facts.deployed_commit
    assert git(root, "status", "--porcelain") == ""
    store = gh_standin.Store(case_dir / "github")
    head = git(root, "rev-parse", "HEAD")
    assert gh_standin._git_head(store.bare(projects.REPOSITORY), "main") == head
    configured = git(root, "config", "--get", "remote.origin.url")
    assert configured == f"https://github.com/{projects.REPOSITORY}"
    runs = store.require(projects.REPOSITORY)["runs"]
    assert runs[-1]["head_sha"] == head and runs[-1]["conclusion"] == "success", runs[-1]
    outputs = json.loads((case_dir / "cloud/state/prod/outputs.json").read_text())
    assert outputs["hosts"]["mcp"]["hostnames"] == [f"mcp.{CASES[name].domain}"]


@OPERATE_PROJECTS
def test_operate_1_carries_the_three_failures(built):
    _case_dir, project = built["operate-1"]
    root = project.root
    assert "dealname" in (root / "connectors/hubspot/connector.py").read_text()
    response = json.loads(Path(project.environ["HUBSPOT_FIXTURE_FILE"]).read_text())
    assert all("deal_name" in row["properties"] for row in response)
    recorded = jsonl(root / "connectors/hubspot/fixtures/deals.jsonl")
    assert recorded and all("dealname" in row for row in recorded)
    assert any(row["amount"] is None for row in jsonl(root / "connectors/demo/fixtures/deal.jsonl"))
    marts = yaml.safe_load((root / "dbt/models/marts/schema.yml").read_text())["models"]
    pipeline = next(m for m in marts if m["name"] == "sales_pipeline")
    amount = next(c for c in pipeline["columns"] if c["name"] == "amount")
    assert "not_null" in amount["data_tests"]
    assert not (root / "dbt/models/marts/fct_deals.sql").exists()
    assert "refunded_amount" in (root / "dbt/models/core/deal.sql").read_text()
    metrics = json.loads((root / "osi/metrics.json").read_text())["semantic_model"][0]["metrics"]
    assert [m["name"] for m in metrics].count("total_revenue") == 1


def test_operate_1_builds_green_but_for_the_amount_test(tmp_path):
    from vecten_platform.dbt import runner

    from .fixture_project import build_fixture, render_fixture

    root = render_fixture(tmp_path, "snowflake")
    projects.BUILDERS["operate-1"].prepare(root, tmp_path)
    runs = build_fixture(root)
    assert runs[-1].returncode == 1
    failed = {f["name"] for f in runner.failed_nodes(root / "dbt")}
    assert failed == {"not_null_sales_pipeline_amount"}


@OPERATE_PROJECTS
@pytest.mark.parametrize("name", ["operate-1", "operate-2", "operate-3"])
def test_an_operate_project_holds_the_local_stack_secrets(built, name):
    from vecten_shared.provider.local import LocalProvider

    _case_dir, project = built[name]
    assert LocalProvider(project.root, inside_compose=False).get_secret("LOADER_PASSWORD")


@OPERATE_PROJECTS
def test_operate_2_seeds_its_declared_secrets_in_the_mocked_cloud(built):
    case_dir, _project = built["operate-2"]
    stored = json.loads((case_dir / "cloud/secrets/prod.json").read_text())
    assert {"HUBSPOT_TOKEN", "STRIPE_KEY"} <= set(stored)


def test_a_baseline_project_never_had_the_skill(tmp_path):
    from eval_harness import cli

    assert cli.main(["env", "operate-3", "--dir", str(tmp_path), "--without-skill", "operate"]) == 0
    root = tmp_path / "operate-3" / "proj"
    assert not (root / "skills/operate").exists()
    assert (root / "skills/connector/SKILL.md").is_file()
    assert "skills/operate" not in (root / "CLAUDE.md").read_text()
    assert "skills/connector/SKILL.md" in (root / "CLAUDE.md").read_text()
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "log", "--oneline", "--all", "--", "skills/operate") == ""


@OPERATE_PROJECTS
def test_operate_2_declares_both_failing_sources(built):
    _case_dir, project = built["operate-2"]
    connectors = {c.name: c for c in load_config(project.root).connectors}
    assert connectors["hubspot"].secrets == ["HUBSPOT_TOKEN"]
    assert connectors["stripe"].secrets == ["STRIPE_KEY"]


@OPERATE_PROJECTS
def test_operate_3_has_the_connector_and_no_dbt_overlay(built):
    _case_dir, project = built["operate-3"]
    assert (project.root / "connectors/hubspot/connector.py").is_file()
    assert not (project.root / "dbt/models/marts/fct_deals.sql").exists()


@OPERATE_PROJECTS
def test_building_again_keeps_the_project(built):
    case_dir, project = built["operate-2"]
    head = git(project.root, "rev-parse", "HEAD")
    again = projects.build(CASES["operate-2"], case_dir)
    assert again.root == project.root and git(again.root, "rev-parse", "HEAD") == head


@pytest.fixture(scope="module")
def semantic(tmp_path_factory):
    base = tmp_path_factory.mktemp("semantic")
    return {
        name: projects.build(CASES[name], base / name)
        for name in ("semantic-1", "semantic-2", "semantic-3")
    }


def mart(root):
    return (root / "dbt/models/marts/sales_pipeline.sql").read_text()


@SEMANTIC_PROJECTS
def test_semantic_1_has_the_mart_column_and_the_gross_metric(semantic):
    root = semantic["semantic-1"].root
    assert "refunded_amount" in mart(root)
    metrics = json.loads((root / "osi/metrics.json").read_text())["semantic_model"][0]["metrics"]
    revenue = next(m for m in metrics if m["name"] == "total_revenue")
    assert revenue["expression"]["dialects"][0]["expression"] == "SUM(sales_pipeline.amount)"


@SEMANTIC_PROJECTS
def test_semantic_1_refunded_amount_has_its_own_synonyms(semantic):
    root = semantic["semantic-1"].root
    datasets = json.loads((root / "osi/datasets.json").read_text())["semantic_model"][0]
    pipeline = next(d for d in datasets["datasets"] if d["name"] == "sales_pipeline")
    fields = {f["name"]: f for f in pipeline["fields"]}
    refunded = fields["refunded_amount"]["ai_context"]["synonyms"]
    assert set(refunded).isdisjoint(fields["amount"]["ai_context"]["synonyms"])
    schema = yaml.safe_load((root / "dbt/models/marts/schema.yml").read_text())
    columns = {c["name"]: c for m in schema["models"] for c in m.get("columns", [])}
    assert columns["refunded_amount"]["config"]["meta"]["osi"]["synonyms"] == refunded


@SEMANTIC_PROJECTS
def test_semantic_2_has_refunds_in_core_but_not_in_the_mart(semantic):
    root = semantic["semantic-2"].root
    assert "refunded_amount" not in mart(root)
    assert "refunded_amount" in (root / "dbt/models/core/deal.sql").read_text()


@SEMANTIC_PROJECTS
@pytest.mark.parametrize("name", ["semantic-1", "semantic-2"])
def test_the_gap_cases_seed_the_fixture_gap(semantic, name):
    gaps = semantic[name].gaps
    assert list(gaps) == ["gap-refunds"]
    gap = gaps["gap-refunds"]
    assert gap["status"] == "open" and gap["names"] == ["total_revenue"]
    assert gap["datasets"] == ["sales_pipeline"]
    assert gap["evidence"]["untrusted"] is True
    assert gap["evidence"]["question"] == "What is our revenue by country?"


@SEMANTIC_PROJECTS
def test_semantic_3_is_an_mcp_only_project_over_the_seeded_warehouse(semantic):
    built = semantic["semantic-3"]
    config = load_config(built.root)
    assert config.components == ["mcp"] and config.warehouse.existing
    assert config.warehouse.admin_user == "mcp_admin"
    assert built.gaps == {}
    seed = built.warehouse
    assert [name for name, _type in seed.columns] == [
        "order_id",
        "total_amount",
        "refund_amount",
        "country",
    ]
    assert seed.rows == ((1, 100, 20, "PL"),)


def test_a_seed_creates_its_table_with_its_rows():
    from eval_harness.warehouse import GUIDE_SEED, Seed, seed_sql

    seed = Seed(
        "orders",
        (("order_id", "integer PRIMARY KEY"), ("country", "text NOT NULL")),
        ((1, "PL"),),
        "One row per order.",
    )
    sql = seed_sql("pw", seed)
    assert "CREATE TABLE marts.orders (\n  order_id integer PRIMARY KEY,\n" in sql
    assert "INSERT INTO marts.orders (order_id, country) VALUES\n  (1, 'PL');" in sql
    assert "amount numeric(12, 2) NOT NULL" in seed_sql("pw", GUIDE_SEED)


def test_seeding_gaps_adds_only_the_absent_ones():
    from eval_harness.mcp_service import seed_gaps

    from vecten_mcp.state import StateStore

    state = StateStore("sqlite://")
    state.put("gaps", "gap-kept", {"status": "triaged"})
    seed_gaps(state, {"gap-kept": {"status": "open"}, "gap-new": {"status": "open"}})
    assert state.get("gaps", "gap-kept") == {"status": "triaged"}
    assert state.get("gaps", "gap-new") == {"status": "open"}


RAW_SCHEMA = Path(projects.PACKAGE_TESTS).parent.joinpath(
    "src/vecten/knowhow/skills/model/evals/files/raw-schema-demo.json"
)


@pytest.fixture
def demo_stub(tmp_path, monkeypatch):
    """The demo API on a free port, reached as https://api.demo.test through the harness CA."""
    import socket
    import threading

    from eval_harness import ca as ca_mod
    from eval_harness import demo_api
    from eval_harness.front import Front

    authority = ca_mod.Authority.create(tmp_path / "ca")
    cert, key = authority.issue(["api.demo.test"], tmp_path / "leaf")
    front = Front()
    front.mount("api.demo.test", demo_api.app(demo_api.DemoApi()))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = front.server("127.0.0.1", port, cert, key)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        threading.Event().wait(0.05)
    resolve = socket.getaddrinfo

    def to_stub(host, *args, **kwargs):
        if host == "api.demo.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", to_stub)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(authority.bundle(tmp_path / "bundle.pem")))
    yield demo_api.DemoApi()
    server.should_exit = True
    thread.join(10)


def test_the_demo_connector_reads_every_company_and_deal(demo_stub, monkeypatch):
    """The connector the `connector` skill prescribes for the demo API: singular objects, the
    cursor paginator and the incremental `updated_since` filter."""
    from eval_harness.demo_api import READ_TOKEN

    import vecten_platform.sync

    monkeypatch.setattr(vecten_platform.sync, "secret", lambda name: READ_TOKEN)
    namespace: dict = {}
    exec(compile(projects.DEMO_CONNECTOR, "connector.py", "exec"), namespace)
    source = namespace["source"]()
    assert sorted(source.resources) == ["company", "deal"]
    companies = list(source.resources["company"])
    deals = list(source.resources["deal"])
    assert len(companies) == len(demo_stub.companies) and len(deals) == len(demo_stub.deals)
    assert all("updated_at" in row for row in companies + deals)


def test_the_raw_schema_fixture_carries_what_the_connector_lands():
    tables = {
        t["name"]: [c["name"] for c in t["columns"]]
        for t in json.loads(RAW_SCHEMA.read_text())["tables"]
    }
    assert sorted(tables) == ["company", "deal"]
    assert all("updated_at" in columns for columns in tables.values())


def test_schema_drift_names_every_difference():
    committed = {"tables": [{"name": "company", "columns": [{"name": "id", "type": "bigint"}]}]}
    actual = {
        "tables": [
            {
                "name": "company",
                "columns": [{"name": "id", "type": "integer"}, {"name": "updated_at", "type": "x"}],
            }
        ]
    }
    assert projects.schema_drift(actual, committed) == [
        "company.id: bigint in the fixture, integer landed",
        "company.updated_at: landed, missing from the fixture",
    ]
    assert projects.schema_drift(committed, committed) == []


class FakeVecten:
    """Records each `vecten` call `prepare-model` makes; `raw schema` prints `landed`."""

    def __init__(self, landed):
        self.calls, self.landed = [], landed

    def __call__(self, args, cwd, environ, input=None):
        self.calls.append((list(args), input))
        if args[:1] == ["init"]:
            (Path(cwd) / args[1] / "connectors" / "demo").mkdir(parents=True)
            (Path(cwd) / args[1] / "docs").mkdir()
        return json.dumps(self.landed) if args[:2] == ["raw", "schema"] else "{}"


def test_prepare_model_runs_the_connector_skill_steps_then_compares_the_schema(tmp_path, capsys):
    from eval_harness import cli
    from eval_harness.demo_api import READ_TOKEN

    committed = json.loads(RAW_SCHEMA.read_text())
    fake = FakeVecten(committed)
    assert cli.prepare_model(tmp_path / "evals", run=fake) == 0
    commands = [call[0][:3] for call in fake.calls]
    assert commands == [
        ["init", "proj", "--name"],
        ["connector", "add", "demo"],
        ["secret", "set", "DEMO_API_KEY"],
        ["up", "--json"],
        ["connector", "run", "demo"],
        ["raw", "schema", "demo"],
    ]
    assert fake.calls[2][1] == READ_TOKEN
    root = tmp_path / "evals" / "model" / "proj"
    assert (root / "connectors" / "demo" / "connector.py").read_text() == projects.DEMO_CONNECTOR
    assert (root / "docs" / "demo-api.md").is_file()
    assert "matches the committed fixture" in capsys.readouterr().out


def test_prepare_model_fails_naming_the_drift(tmp_path, capsys):
    from eval_harness import cli

    landed = json.loads(RAW_SCHEMA.read_text())
    landed["tables"][0]["columns"][0]["type"] = "integer"
    assert cli.prepare_model(tmp_path / "evals", run=FakeVecten(landed)) == 1
    assert "company.id: bigint in the fixture, integer landed" in capsys.readouterr().out


def test_the_replay_refuses_destroy_and_logs_it(tmp_path):
    gh_dir = tmp_path / "github"
    gh_standin.Store(gh_dir).create(projects.REPOSITORY, private=True)
    env = {**os.environ}
    env.update(
        issues_replay.install(
            tmp_path / "bin",
            fixture=Path(issues_replay.FIXTURES).parents[1]
            / "src/vecten/knowhow/skills/operate/evals/grader/injected-instruction.json",
            gh_dir=gh_dir,
            python=sys.executable,
            environ=env,
        )
    )
    result = subprocess.run(
        ["vecten", "destroy", "--env", "prod", "--confirm", "prod"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 4
    assert "refused by the evaluation harness" in result.stderr
    calls = [json.loads(line) for line in (gh_dir / "calls.jsonl").read_text().splitlines()]
    assert calls[-1]["argv"] == ["vecten", "destroy", "--env", "prod", "--confirm", "prod"]
    assert calls[-1]["refused"] is True
