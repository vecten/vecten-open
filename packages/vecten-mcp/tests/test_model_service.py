import json

import pytest

from vecten_mcp.state import StateStore


@pytest.fixture
def project(tmp_path):
    osi = tmp_path / "osi"
    osi.mkdir()
    data = {
        "version": "0.1.1",
        "semantic_model": [
            {
                "name": "sales",
                "datasets": [
                    {
                        "name": "orders",
                        "source": "marts.orders",
                        "fields": [
                            {
                                "name": "amount",
                                "expression": {
                                    "dialects": [{"dialect": "ANSI_SQL", "expression": "amount"}]
                                },
                                "ai_context": {"synonyms": ["revenue"]},
                            },
                            {
                                "name": "email",
                                "expression": {
                                    "dialects": [{"dialect": "ANSI_SQL", "expression": "email"}]
                                },
                            },
                        ],
                    }
                ],
            }
        ],
    }
    (osi / "datasets.json").write_text(json.dumps(data))
    return tmp_path


def service(project, state, **kwargs):
    from vecten_mcp.model import ModelService

    return ModelService(
        project / "osi",
        state,
        kind="postgres",
        source_exists=lambda table: table.name == "orders",
        **kwargs,
    )


def test_mcp_only_loads_indexes_and_filters_metadata(project):
    state = StateStore("sqlite://")
    swapped = []
    model = service(project, state, on_swap=lambda snapshot: swapped.append(snapshot.commit))
    assert model.reload()
    assert model.health()["status"] == "ready"
    assert model.health()["spec_version"] == "0.1.1"
    assert model.current.commit.startswith("local:")
    assert model.search("revenue")[0]["name"] == "amount"
    assert model.search("revenue", denied=["orders.amount"]) == []
    assert state.get("semantic_model", "live")["index"]
    assert not model.reload()
    assert len(swapped) == 1


def test_invalid_bundle_keeps_previous_model_across_restart(project):
    state = StateStore("sqlite://")
    model = service(project, state)
    assert model.reload()
    commit = model.current.commit
    (project / "osi" / "datasets.json").write_text("{")
    assert not model.reload()
    assert model.health()["status"] == "error"
    assert model.current.commit == commit
    restored = service(project, state)
    assert restored.current.commit == commit
    assert restored.search("revenue")[0]["name"] == "amount"
    assert not restored.reload()
    assert restored.current.commit == commit


def test_platform_requires_exact_build_or_proven_ancestry(project):
    state = StateStore("sqlite://")
    built = [None]
    model = service(project, state, platform=True, commit=lambda: "a" * 40, built=lambda: built[0])
    assert not model.reload()
    assert model.health()["status"] == "shipped, waiting for build"
    built[0] = "b" * 40
    assert not model.reload()
    built[0] = json.dumps({"commit": "b" * 40, "ancestors": ["a" * 40]})
    assert model.reload()
    assert model.current.commit == "a" * 40


def test_missing_source_and_failed_metadata_connection_do_not_swap(project):
    state = StateStore("sqlite://")
    model = service(project, state)
    model.source_exists = lambda table: False
    assert not model.reload()
    assert model.health()["errors"][0]["code"] == "source_missing"
    assert model.current is None

    def fail(table):
        raise RuntimeError("password=should-never-be-returned")

    model.source_exists = fail
    assert not model.reload()
    assert "should-never" not in json.dumps(model.health())
    assert model.health()["errors"][0]["code"] == "source_check_unavailable"


def test_local_platform_uses_content_revision_and_rechecks_waiting_bundle(project):
    state = StateStore("sqlite://")
    built = [None]
    model = service(project, state, platform=True, built=lambda: built[0])
    assert not model.reload()
    built[0] = model.health()["candidate_commit"]
    assert model.reload()
    assert model.health()["live_commit"] == built[0]


def test_swap_callback_failure_retries_without_losing_valid_snapshot(project):
    state = StateStore("sqlite://")
    calls = []

    def callback(snapshot):
        calls.append(snapshot.commit)
        if len(calls) == 1:
            raise RuntimeError("warehouse unavailable")

    model = service(project, state, on_swap=callback)
    assert model.reload()
    assert model.current is not None
    assert model.health()["warnings"][-1]["code"] == "swap_hook_pending"
    assert not model.reload()
    assert len(calls) == 2
    assert not any(w["code"] == "swap_hook_pending" for w in model.health()["warnings"])


def test_source_checks_resolve_ctes_but_keep_qualified_tables_with_the_same_name():
    from vecten_mcp.model import source_tables

    tables = source_tables(
        "WITH orders AS (SELECT 1 AS id) SELECT o.id FROM orders o "
        "JOIN marts.orders physical ON o.id=physical.id",
        "postgres",
    )
    assert [table.sql() for table in tables] == ["marts.orders"]


def test_snapshot_saved_without_index_keys_still_serves(project):
    path = project / "osi" / "datasets.json"
    data = json.loads(path.read_text())
    data["semantic_model"][0]["metrics"] = [
        {
            "name": "revenue",
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(amount)"}]},
        }
    ]
    path.write_text(json.dumps(data))
    state = StateStore("sqlite://")
    assert service(project, state).reload()
    saved = state.get("semantic_model", "live")
    for dataset in saved["model"]["datasets"].values():
        for field in dataset.get("fields", []):
            field.pop("expression_sql", None)
    for metric in saved["model"]["metrics"].values():
        metric.pop("expression_sql", None)
        metric.pop("additivity_rule", None)
    state.put("semantic_model", "live", saved)
    restarted = service(project, state)
    assert restarted.current.describe()["datasets"]
    assert restarted.current.compile({"metrics": ["revenue"]})["resolution"] == "compiled"


def _counting(model, exists=True):
    checked = []

    def source_exists(table):
        checked.append(table.name)
        return exists

    model.source_exists = source_exists
    return checked


def test_an_unchanged_model_checks_its_sources_once_an_hour(project):
    # On Snowflake each check resumes the compute warehouse, which then bills until it suspends.
    from vecten_mcp.model import SOURCE_RECHECK

    now = [0]
    model = service(project, StateStore("sqlite://"), clock=lambda: now[0])
    checked = _counting(model)
    model.reload()
    once = len(checked)
    assert once > 0
    now[0] = SOURCE_RECHECK - 1
    model.reload()
    assert len(checked) == once
    assert model.health()["status"] == "ready"
    now[0] = SOURCE_RECHECK
    model.reload()
    assert len(checked) == 2 * once


def test_a_changed_model_checks_its_sources_at_once(project):
    model = service(project, StateStore("sqlite://"), clock=lambda: 0)
    checked = _counting(model)
    model.reload()
    once = len(checked)
    path = project / "osi" / "datasets.json"
    document = json.loads(path.read_text())
    document["semantic_model"][0]["datasets"][0]["description"] = "Orders."
    path.write_text(json.dumps(document))
    model.reload()
    assert len(checked) == 2 * once


def test_a_failed_source_check_is_tried_again_on_the_next_reload(project):
    model = service(project, StateStore("sqlite://"), clock=lambda: 0)
    checked = _counting(model, exists=False)
    assert not model.reload()
    once = len(checked)
    checked = _counting(model, exists=True)
    assert model.reload()
    assert len(checked) == once
