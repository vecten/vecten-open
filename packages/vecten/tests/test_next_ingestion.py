import json
from pathlib import Path

from vecten.next.ingestion import access_apply_items, access_ask_item, ingestion_items
from vecten.next.items import Item
from vecten_shared.config import VectenConfig
from vecten_shared.records import write_record


def config(**overrides):
    base = {
        "users": 3,
        "project": {"name": "acme", "scaffold_version": "0.1.0"},
        "wanted": [],
        "connectors": [],
    }
    return VectenConfig.model_validate({**base, **overrides})


def declare(root: Path, name: str) -> None:
    pkg = root / "connectors" / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "connector.py").write_text("def source():\n    return None\n")


def test_item_json_shape_for_ask_and_command():
    ask = Item("ask", "t", "q?", answer_keys=("access.x",), allowed=("default",))
    assert ask.to_json() == {
        "kind": "ask",
        "title": "t",
        "resolution": "q?",
        "answer_keys": ["access.x"],
        "allowed": ["default"],
        "show_first": [],
    }
    cmd = Item("command", "t", "vecten up")
    assert cmd.to_json() == {"kind": "command", "title": "t", "resolution": "vecten up"}
    json.dumps(ask.to_json())


def test_wanted_source_is_a_connector_skill_item(tmp_path):
    items = ingestion_items(tmp_path, config(wanted=["hubspot"]))
    assert [(i.kind, i.resolution) for i in items] == [("skill", "connector")]
    assert "hubspot" in items[0].title


def test_declared_without_directory_resolves_to_add(tmp_path):
    items = ingestion_items(tmp_path, config(connectors=[{"name": "hubspot", "source": "dlt"}]))
    assert items[0].kind == "command" and items[0].resolution == "vecten connector add hubspot"


def test_access_asked_before_the_local_run_item(tmp_path):
    declare(tmp_path, "hubspot")
    cfg = config(
        connectors=[
            {"name": "hubspot", "source": "dlt"},
            {"name": "stripe", "source": "rest", "access": {"groups": ["finance"]}},
        ]
    )
    declare(tmp_path, "stripe")
    items = ingestion_items(tmp_path, cfg)
    kinds = [(i.kind, i.resolution) for i in items]
    assert kinds[0][0] == "ask" and items[0].answer_keys == ("access.hubspot",)
    assert items[0].allowed == ("default", "finance")
    assert "raw_hubspot" in items[0].resolution
    assert kinds[1:] == [
        ("command", "vecten connector run hubspot --record"),
        ("command", "vecten connector run stripe --record"),
    ]


def _ok_record(root: Path, name: str) -> None:
    write_record(
        root,
        f"connector-run-{name}",
        {
            "source": name,
            "ok": True,
            "recorded": False,
            "objects": {"companies": {"ok": True, "rows": 1, "error": None}},
        },
    )


def _fixture(root: Path, name: str) -> None:
    (root / "connectors" / name / "fixtures").mkdir(exist_ok=True)
    (root / "connectors" / name / "fixtures" / "companies.jsonl").write_text('{"id": 1}\n')


def test_run_then_fixtures_then_nothing(tmp_path):
    declare(tmp_path, "hubspot")
    cfg = config(connectors=[{"name": "hubspot", "source": "dlt", "access": {"groups": ["sales"]}}])
    assert [i.resolution for i in ingestion_items(tmp_path, cfg)] == [
        "vecten connector run hubspot --record"
    ]
    write_record(
        tmp_path,
        "connector-run-hubspot",
        {"source": "hubspot", "ok": False, "recorded": False, "objects": {}},
    )
    assert [i.resolution for i in ingestion_items(tmp_path, cfg)] == [
        "vecten connector run hubspot --record"
    ]
    _ok_record(tmp_path, "hubspot")
    assert [i.resolution for i in ingestion_items(tmp_path, cfg)] == [
        "vecten connector run hubspot --record"
    ]
    assert ingestion_items(tmp_path, cfg)[0].title == "Record fixtures for hubspot"
    _fixture(tmp_path, "hubspot")
    assert ingestion_items(tmp_path, cfg) == []


def test_run_item_without_record_when_fixtures_exist(tmp_path):
    declare(tmp_path, "hubspot")
    _fixture(tmp_path, "hubspot")
    cfg = config(connectors=[{"name": "hubspot", "source": "dlt", "access": {"groups": ["sales"]}}])
    items = ingestion_items(tmp_path, cfg)
    assert [(i.title, i.resolution) for i in items] == [
        ("Run hubspot locally", "vecten connector run hubspot")
    ]


def test_run_items_come_before_fixture_items(tmp_path):
    declare(tmp_path, "alpha")
    declare(tmp_path, "beta")
    _ok_record(tmp_path, "alpha")
    cfg = config(
        connectors=[
            {"name": "alpha", "source": "dlt", "access": {"groups": ["sales"]}},
            {"name": "beta", "source": "dlt", "access": {"groups": ["sales"]}},
        ]
    )
    assert [i.title for i in ingestion_items(tmp_path, cfg)] == [
        "Run beta locally",
        "Record fixtures for alpha",
    ]


def test_access_apply_item_only_after_deploy_and_until_applied(tmp_path):
    cfg = config(
        connectors=[{"name": "hubspot", "source": "dlt", "access": {"groups": ["sales", "ops"]}}]
    )
    assert access_apply_items(tmp_path, cfg, "prod") == []
    cfg.environments["prod"].deployed_commit = "abc123"
    items = access_apply_items(tmp_path, cfg, "prod")
    assert len(items) == 1 and items[0].kind == "command"
    assert "vecten group add sales --env prod" in items[0].resolution
    assert "vecten grant --schema raw_hubspot --group ops --env prod" in items[0].resolution
    cfg.environments["prod"].connector_access_applied = ["hubspot"]
    assert access_apply_items(tmp_path, cfg, "prod") == []


def test_access_apply_items_on_an_unknown_env_adds_no_environment(tmp_path):
    cfg = config(connectors=[{"name": "hubspot", "source": "dlt", "access": {"groups": ["sales"]}}])
    assert access_apply_items(tmp_path, cfg, "staging") == []
    assert "staging" not in cfg.environments


def test_access_ask_item_offers_default_and_known_groups():
    cfg = config(
        connectors=[
            {"name": "a", "source": "dlt"},
            {"name": "b", "source": "dlt", "access": {"groups": ["sales", "default"]}},
        ]
    )
    item = access_ask_item(cfg, cfg.connector("a"))
    assert item.allowed == ("default", "sales") and item.kind == "ask"
