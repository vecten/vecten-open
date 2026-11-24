import json
import shutil
import tomllib
from pathlib import Path

import dlt
import pytest
import yaml

from vecten.connectors import run as run_mod
from vecten.connectors.render import scaffold_hash
from vecten_shared.config import ConnectorDecl, load_config, save_config
from vecten_shared.records import read_record

PLATFORM_TESTS = (
    Path(__file__).resolve().parents[2] / "vecten-platform" / "tests" / "fixture_connector" / "demo"
)
LOCAL = ("--name", "acme", "--users", "3", "--source", "demo")


@pytest.fixture
def project(invoke, workdir, monkeypatch, tmp_path):
    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    root = workdir / "p"
    monkeypatch.chdir(root)
    # The fixture connector stands in for a source the user wrote; register it by hand.
    shutil.copytree(PLATFORM_TESTS, root / "connectors" / "demo")
    (root / "connectors" / "demo" / "fixtures").mkdir(exist_ok=True)
    config = load_config(root)
    config.wanted = []
    config.connectors.append(ConnectorDecl(name="demo", source="custom", secrets=["DEMO_TOKEN"]))
    save_config(root, config)
    (root / ".env").write_text("DEMO_TOKEN=t\nLOADER_PASSWORD=unused\n")
    dest = dlt.destinations.duckdb(str(tmp_path / "wh.duckdb"))
    monkeypatch.setattr(run_mod, "make_destination", lambda ctx: dest)
    return root


def _connector_decl(project: Path) -> dict:
    return tomllib.loads((project / "vecten.toml").read_text())["connectors"][0]


def test_run_all_objects_reports_rows_and_fails_on_the_bad_one(invoke, project):
    r = invoke("connector", "run", "demo", "--json")
    assert r.exit_code == 5, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["ok"] is False
    assert doc["objects"]["good_a"] == {"ok": True, "rows": 2, "error": None}
    assert doc["objects"]["good_b"]["ok"] is True
    assert doc["objects"]["bad"]["ok"] is False
    assert "upstream returned 500" in doc["objects"]["bad"]["error"]
    record = read_record(project, "connector-run-demo")
    assert record["ok"] is False and record["objects"]["good_a"]["ok"] is True
    sources = yaml.safe_load((project / "dbt" / "models" / "staging" / "sources.yml").read_text())
    demo = next(s for s in sources["sources"] if s["name"] == "demo")
    assert [t["name"] for t in demo["tables"]] == ["good_a", "good_b"]


def test_run_single_object(invoke, project):
    r = invoke("connector", "run", "demo", "--object", "good_a", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert list(doc["objects"]) == ["good_a"] and doc["objects"]["good_a"]["rows"] == 2
    record = read_record(project, "connector-run-demo")
    assert record["ok"] is True and list(record["objects"]) == ["good_a"]
    human = invoke("connector", "run", "demo", "--object", "good_a")
    assert "good_a: 0 rows" in human.stdout


def test_run_unknown_object_is_usage(invoke, project):
    r = invoke("connector", "run", "demo", "--object", "nope", "--json")
    assert r.exit_code == 2
    assert "good_a, bad, good_b" in json.loads(r.stdout)["error"]


def test_run_record_writes_fixtures(invoke, project):
    r = invoke("connector", "run", "demo", "--object", "good_b", "--record", "--json")
    assert r.exit_code == 0
    assert json.loads(r.stdout)["recorded"] is True
    fixture = project / "connectors" / "demo" / "fixtures" / "good_b.jsonl"
    assert fixture.read_text().splitlines() == ['{"id": 10, "amount": 1.5}']
    assert read_record(project, "connector-run-demo")["recorded"] is True


def test_run_source_not_added_suggests_add(invoke, project):
    r = invoke("connector", "run", "stripe", "--json")
    assert r.exit_code == 4
    assert "vecten connector add stripe" in json.loads(r.stdout)["error"]


def test_run_without_a_name_is_usage(invoke, project):
    r = invoke("connector", "run", "--json")
    assert r.exit_code == 2
    assert "source name" in json.loads(r.stdout)["error"]


def test_run_bad_name_is_usage(invoke, project):
    r = invoke("connector", "run", "../demo", "--json")
    assert r.exit_code == 2


def test_run_missing_secret_is_prerequisite(invoke, project):
    (project / ".env").write_text("LOADER_PASSWORD=unused\n")
    r = invoke("connector", "run", "demo", "--json")
    assert r.exit_code == 3
    assert "vecten secret set DEMO_TOKEN" in json.loads(r.stdout)["error"]
    assert read_record(project, "connector-run-demo") is None


def test_run_source_construction_failure(invoke, project):
    (project / "connectors" / "demo" / "connector.py").write_text(
        "def source():\n    raise ValueError('bad config')\n"
    )
    r = invoke("connector", "run", "demo", "--json")
    assert r.exit_code == 5
    doc = json.loads(r.stdout)
    assert "ValueError" in doc["error"] and "bad config" in doc["error"]
    record = read_record(project, "connector-run-demo")
    assert record["ok"] is False and record["objects"] == {}


CRM_CONNECTOR = """
import dlt


@dlt.resource(name="Contact-Lists")
def contact_lists():
    yield from [{"id": 1, "members": [{"m": "a"}, {"m": "b"}]}, {"id": 2, "members": []}]


@dlt.source(name="demo")
def demo_source():
    return [contact_lists]


def source():
    return demo_source()
"""


def test_run_adds_the_tables_that_landed_to_sources_yml(invoke, project):
    (project / "connectors" / "demo" / "connector.py").write_text(CRM_CONNECTOR)
    r = invoke("connector", "run", "demo", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["objects"]["Contact-Lists"]["rows"] == 2
    assert doc["tables"] == ["contact_lists"]
    sources = yaml.safe_load((project / "dbt" / "models" / "staging" / "sources.yml").read_text())
    demo = next(s for s in sources["sources"] if s["name"] == "demo")
    assert [t["name"] for t in demo["tables"]] == ["contact_lists"]


def test_run_of_one_object_keeps_the_other_objects_in_the_record(invoke, project):
    assert invoke("connector", "run", "demo", "--json").exit_code == 5
    r = invoke("connector", "run", "demo", "--object", "good_a", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert list(json.loads(r.stdout)["objects"]) == ["good_a"]
    record = read_record(project, "connector-run-demo")
    assert set(record["objects"]) == {"good_a", "bad", "good_b"}
    assert record["objects"]["bad"]["ok"] is False
    assert record["objects"]["good_a"] == {"ok": True, "rows": 0, "error": None}
    assert record["ok"] is False


def closed_port_destination(ctx):
    from vecten_platform.sync.destination import build_destination
    from vecten_shared.provider import ConnectionSpec

    spec = ConnectionSpec(
        kind="postgres",
        database="warehouse",
        role="loader",
        auth="password",
        host="127.0.0.1",
        port=1,
    )
    return build_destination(spec, "unused")


def test_run_with_the_warehouse_down_is_prerequisite(invoke, project, monkeypatch):
    monkeypatch.setattr(run_mod, "make_destination", closed_port_destination)
    r = invoke("connector", "run", "demo", "--record", "--json")
    assert r.exit_code == 3, r.stdout + r.stderr
    assert "the warehouse is not reachable; run `vecten up`" in json.loads(r.stdout)["error"]
    assert read_record(project, "connector-run-demo") is None
    assert list((project / "connectors" / "demo" / "fixtures").iterdir()) == []


def test_run_flips_a_generic_scaffold_to_custom_when_edited(invoke, project):
    text = (project / "vecten.toml").read_text()
    text = text.replace('source = "custom"', 'source = "rest"\nscaffold_hash = "0000"')
    (project / "vecten.toml").write_text(text)
    assert invoke("connector", "run", "demo", "--object", "good_a", "--json").exit_code == 0
    decl = _connector_decl(project)
    assert decl["source"] == "custom" and "scaffold_hash" not in decl


def test_run_keeps_a_generic_kind_while_the_scaffold_is_untouched(invoke, project):
    digest = scaffold_hash((project / "connectors" / "demo" / "connector.py").read_text())
    text = (project / "vecten.toml").read_text()
    text = text.replace('source = "custom"', f'source = "rest"\nscaffold_hash = "{digest}"')
    (project / "vecten.toml").write_text(text)
    assert invoke("connector", "run", "demo", "--object", "good_a", "--json").exit_code == 0
    assert _connector_decl(project)["source"] == "rest"


def test_record_limit_stops_the_meter_and_redacts_before_writing(invoke, project, tmp_path):
    import duckdb

    directory = project / "connectors/demo"
    (directory / "connector.py").write_text(
        """import dlt
from pathlib import Path

@dlt.resource(name="events")
def events():
    for page in range(100):
        with Path("requests").open("a") as meter:
            print(page, file=meter)
        for row in range(2):
            with Path("consumed").open("a") as meter:
                print(row, file=meter)
            yield {"id": page * 2 + row, "email": "private@example.test"}

@dlt.source(name="demo")
def source():
    return events

def redact_fixture(object_name, row):
    assert object_name == "events"
    return {**row, "email": "sample@example.test"}
"""
    )
    result = invoke("connector", "run", "demo", "--record", "--limit", "3")
    assert result.exit_code == 0, result.output
    assert (project / "requests").read_text().splitlines() == ["0", "1"]
    assert len((project / "consumed").read_text().splitlines()) == 3
    fixture = directory / "fixtures/events.jsonl"
    assert [json.loads(line)["email"] for line in fixture.read_text().splitlines()] == [
        "sample@example.test"
    ] * 3
    with duckdb.connect(str(tmp_path / "wh.duckdb"), read_only=True) as warehouse:
        assert warehouse.execute("select distinct email from raw_demo.events").fetchall() == [
            ("sample@example.test",)
        ]


def test_synthetic_fixture_check_never_opens_source(invoke, project):
    from vecten_platform.sync.testing import replay_connector_test

    directory = project / "connectors/demo"
    (directory / "connector.py").write_text("raise AssertionError('opened source')")
    config = load_config(project)
    decl = config.connector("demo")
    decl.record = False
    decl.groups = {"daily": ["events"]}
    save_config(project, config)
    result = invoke("connector", "run", "demo", "--record")
    assert result.exit_code == 4 and "synthetic" in result.output
    fixture = directory / "fixtures/events.jsonl"
    fixture.write_text('{"id": 1}\n')
    assert invoke("connector", "check-fixtures", "demo").exit_code == 0
    with pytest.MonkeyPatch.context() as patch:
        assert replay_connector_test(directory, "demo", project / "scratch", patch) == {"events": 1}
    fixture.write_text("42\n")
    result = invoke("connector", "check-fixtures", "demo")
    assert result.exit_code == 6 and "events.jsonl:1" in result.output
    fixture.write_text('{"id": 1}\n')
    (fixture.parent / "typo.jsonl").write_text('{"id": 2}\n')
    result = invoke("connector", "check-fixtures", "demo")
    assert result.exit_code == 6 and "undeclared objects typo" in result.output


@pytest.mark.parametrize(("cooperative", "billed"), [(True, 3), (False, 4)])
def test_paged_recording_trims_last_page(invoke, project, cooperative, billed):
    directory = project / "connectors/demo"
    (directory / "connector.py").write_text(
        """import dlt
from pathlib import Path
from vecten_platform.sync import runtime

@dlt.resource(name="events")
def events():
    consumed = 0
    while consumed < 100:
        size = PAGE_SIZE
        with Path("meter").open("a") as meter:
            print(size, file=meter)
        page = [{"id": n} for n in range(consumed, consumed + size)]
        consumed += size
        yield page

@dlt.source(name="demo")
def source():
    return events
""".replace("PAGE_SIZE", "min(2, runtime().record_limit - consumed)" if cooperative else "2")
    )
    r = invoke("connector", "run", "demo", "--record", "--limit", "3", "--json")
    assert r.exit_code == 0, r.output
    requests = [int(n) for n in (project / "meter").read_text().splitlines()]
    assert len(requests) == 2 and sum(requests) == billed
    assert json.loads(r.stdout)["objects"]["events"]["rows"] == 3
    assert len((directory / "fixtures/events.jsonl").read_text().splitlines()) == 3


@pytest.mark.parametrize(
    "arguments", [["--limit", "2"], ["--record", "--limit", "0"], ["--record", "--limit", "201"]]
)
def test_invalid_recording_limits_stop_before_import(invoke, project, arguments):
    (project / "connectors/demo/connector.py").write_text("raise AssertionError('opened source')")
    assert invoke("connector", "run", "demo", *arguments).exit_code == 2


def test_record_dependent_rest_endpoint_bounds_parent_and_child(invoke, project, monkeypatch):
    from dlt.sources.helpers.rest_client import RESTClient

    requests = []
    (project / "connectors/demo/connector.py").write_text(
        """import dlt
from dlt.sources.rest_api import rest_api_source

def source():
    rest = rest_api_source({
        "client": {"base_url": "https://example.invalid/"},
        "resources": [
            {"name": "parents", "endpoint": "parents"},
            {"name": "children", "endpoint": {"path": "parents/{resources.parents.id}/children"}},
            {"name": "other", "endpoint": "other"},
        ]
    }, name="demo")
    resources = [rest.resources[name] for name in ("parents", "children", "other")]
    return dlt.source(lambda: resources, name="demo")()
"""
    )

    def paginate(self, path, **kwargs):
        for start in range(0, 100, 2):
            requests.append((path, start))
            yield [{"id": start}, {"id": start + 1}]

    monkeypatch.setattr(RESTClient, "paginate", paginate)
    result = invoke("connector", "run", "demo", "--record", "--limit", "3", "--json")
    assert result.exit_code == 0, result.output
    assert {name: obj["rows"] for name, obj in json.loads(result.stdout)["objects"].items()} == {
        "parents": 3,
        "children": 3,
        "other": 3,
    }
    assert [page for path, page in requests if path == "parents"] in ([0, 2, 0], [0, 2, 0, 2])
    child_requests = [(path, page) for path, page in requests if path.endswith("/children")]
    assert len(child_requests) == 2
    assert [page for path, page in requests if path == "other"] == [0, 2]
    assert requests[-1] == ("other", 2)
