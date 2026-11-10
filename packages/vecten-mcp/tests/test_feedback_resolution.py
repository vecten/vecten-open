import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vecten_mcp.state import StateStore

spec = importlib.util.spec_from_file_location(
    "resolution_model_fixture", Path(__file__).with_name("test_model_service.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
project = fixture.project


@pytest.fixture
def resolution(project):
    from vecten_mcp.feedback_resolution import FeedbackResolution

    document = json.loads((project / "osi/datasets.json").read_text())
    document["semantic_model"][0]["metrics"] = [
        {
            "name": "revenue",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.amount)"}]
            },
        }
    ]
    (project / "osi/datasets.json").write_text(json.dumps(document))
    state = StateStore("sqlite://")
    built = ["a" * 40]
    commit = ["a" * 40]
    model = fixture.service(
        project, state, platform=True, commit=lambda: commit[0], built=lambda: built[0]
    )
    model.reload()
    state.put(
        "gaps",
        "gap-1",
        {
            "status": "open",
            "reporters": ["p1"],
            "names": ["revenue"],
            "evidence": {"question": "private"},
        },
    )
    runtime = SimpleNamespace(state=state, model=model)
    manifest = project / "feedback.json"
    service = FeedbackResolution(runtime, manifest, clock=lambda: 100)
    yield SimpleNamespace(
        state=state, model=model, built=built, commit=commit, path=manifest, service=service
    )
    state.close()


def write_manifest(ctx, **overrides):
    entry = {
        "gap": "gap-1",
        "pull_request": "https://github.com/example/project/pull/1",
        "merge_commit": "b" * 40,
        "author": "author",
        "reviewers": ["reviewer"],
        "merged_at": "2026-10-20T10:00:00Z",
        "metrics": ["revenue"],
    }
    data = {"commit": "b" * 40, "ancestors": ["a" * 40], "resolutions": [entry], **overrides}
    ctx.path.write_text(json.dumps(data))
    return data


def test_merge_waits_for_build_then_resolves_and_records_review(resolution):
    c = resolution
    write_manifest(c)
    c.commit[0] = "b" * 40
    assert not c.model.reload()
    c.service.reconcile()
    gap = c.state.get("gaps", "gap-1")
    assert gap["status"] == "open"
    assert gap["delivery_status"] == "merged, waiting for build"
    assert not c.state.scan("gap_notices")
    c.built[0] = "b" * 40
    c.model.reload()
    c.service.reconcile()
    gap = c.state.get("gaps", "gap-1")
    assert gap["status"] == "resolved" and gap["resolved_commit"] == "b" * 40
    assert gap["verification"]["author"] == "author"
    assert gap["verification"]["reviewers"] == ["reviewer"]
    c.service.reconcile()
    assert len(c.state.scan("gap_notices")) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"merge_commit": "c" * 40},
        {"reviewers": []},
        {"author": ""},
        {"pull_request": "javascript:bad"},
        {"reviewers": ["author"]},
    ],
)
def test_invalid_or_unverified_manifest_cannot_resolve(resolution, change):
    c = resolution
    data = write_manifest(c)
    data["resolutions"][0].update(change)
    c.path.write_text(json.dumps(data))
    c.service.reconcile()
    assert c.state.get("gaps", "gap-1")["status"] == "open"
    assert not c.state.get("gaps", "gap-1").get("delivery_status")


def test_notices_wait_for_next_human_session_and_are_private(resolution):
    c = resolution
    write_manifest(c, commit="a" * 40, ancestors=["b" * 40])
    c.service.reconcile()

    def caller(person, session, started, kind="human"):
        c.state.put("sessions", session, {"principal_id": person, "started_at": started})
        return SimpleNamespace(principal={"id": person, "kind": kind}, session_id=session)

    assert c.service.notices(caller("p1", "old", 90)) == []
    assert c.service.notices(caller("p2", "foreign", 110)) == []
    assert c.service.notices(caller("p1", "robot", 110, "service")) == []
    notices = c.service.notices(caller("p1", "new", 110))
    assert len(notices) == 1 and notices[0]["definitions"] == ["revenue"]
    assert "private" not in json.dumps(notices)
    assert c.service.notices(caller("p1", "new", 110)) == []


def test_restart_and_late_gap_do_not_lose_resolution(resolution):
    from vecten_mcp.feedback_resolution import FeedbackResolution

    c = resolution
    c.state.remove("gaps", "gap-1")
    write_manifest(c, commit="a" * 40, ancestors=["b" * 40])
    c.service.reconcile()
    c.state.put("gaps", "gap-1", {"status": "open", "reporters": ["p1"]})
    restarted = FeedbackResolution(c.service.runtime, c.path, clock=lambda: 100)
    restarted.reconcile()
    assert c.state.get("gaps", "gap-1")["status"] == "resolved"


def test_manifest_for_other_bundle_does_not_change_current_gap(resolution):
    c = resolution
    write_manifest(c)
    c.service.reconcile()
    assert c.state.get("gaps", "gap-1")["status"] == "open"


def test_malformed_manifest_does_not_stop_runtime_start(resolution):
    from vecten_mcp.runtime import Runtime

    c = resolution
    c.path.write_text("{broken")
    runtime = Runtime(c.state, None, None, None, None, model=c.model, feedback_resolution=c.service)
    import asyncio

    async def start():
        runtime.start()
        runtime.maintenance_task.cancel()
        try:
            await runtime.maintenance_task
        except asyncio.CancelledError:
            pass

    asyncio.run(start())
    assert c.model.current is not None


def test_reconciliation_does_not_undo_later_admin_status_change(resolution):
    c = resolution
    write_manifest(c, commit="a" * 40, ancestors=["b" * 40])
    c.service.reconcile()
    gap = c.state.get("gaps", "gap-1")
    gap["status"] = "triaged"
    c.state.put("gaps", "gap-1", gap)
    c.service.reconcile()
    assert c.state.get("gaps", "gap-1")["status"] == "triaged"


def test_missing_definition_cannot_resolve_gap(resolution):
    c = resolution
    write_manifest(c, commit="a" * 40, ancestors=["b" * 40])
    c.model.current.metrics.clear()
    c.service.reconcile()
    assert c.state.get("gaps", "gap-1")["status"] == "open"
    assert not c.state.scan("gap_notices")


def test_reconcile_does_not_rewrite_live_model_or_gap_without_changes(resolution, monkeypatch):
    c = resolution
    write_manifest(c, commit="a" * 40, ancestors=["b" * 40])
    c.service.reconcile()
    original = c.model.current
    writes = []
    put = c.state.put

    def record(kind, *args, **kwargs):
        writes.append(kind)
        return put(kind, *args, **kwargs)

    monkeypatch.setattr(c.state, "put", record)
    c.service.reconcile()
    assert not writes
    assert c.model.current is original


def test_notice_poll_without_candidates_never_acquires_write_lock(resolution):
    from sqlalchemy import event

    c = resolution
    c.state.put("sessions", "new", {"principal_id": "p1", "started_at": 110})
    statements = []
    event.listen(
        c.state.engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, *rest: statements.append(statement),
    )
    who = SimpleNamespace(principal={"id": "p1", "kind": "human"}, session_id="new")
    assert c.service.notices(who) == []
    assert not any("BEGIN IMMEDIATE" in s or "FOR UPDATE" in s for s in statements)
    notice_queries = [s for s in statements if "FROM records" in s and "SELECT" in s]
    assert any("JSON_EXTRACT" in s for s in notice_queries)


def test_expired_notices_are_not_recreated_by_reconciliation(resolution):
    c = resolution
    write_manifest(c, commit="a" * 40, ancestors=["b" * 40])
    c.service.reconcile()
    assert len(c.state.scan("gap_notices")) == 1
    c.state.prune(100 + 91 * 86400)
    assert c.state.scan("gap_notices") == []
    c.service.reconcile()
    assert c.state.scan("gap_notices") == []


def test_shared_metric_keeps_latest_review_without_repeated_rewrites(resolution):
    c = resolution
    data = write_manifest(c, commit="a" * 40, ancestors=["b" * 40, "c" * 40])
    older = {
        **data["resolutions"][0],
        "gap": "gap-older",
        "merge_commit": "c" * 40,
        "merged_at": "2026-10-19T10:00:00Z",
    }
    data["resolutions"].append(older)
    c.path.write_text(json.dumps(data))
    c.service.reconcile()
    assert c.model.current.metrics["revenue"]["verified"]["merge_commit"] == "b" * 40
    original = c.model.current
    c.service.reconcile()
    assert c.model.current is original


UNREVIEWED = "merged without an approving review; the gap stays open"


def write_unreviewed(ctx, merge_commit="b" * 40, **entry_overrides):
    entry = {
        "gap": "gap-1",
        "pull_request": "https://github.com/example/project/pull/1",
        "merge_commit": merge_commit,
        "author": "author",
        "merged_at": "2026-10-20T10:00:00Z",
        "metrics": ["revenue"],
        **entry_overrides,
    }
    data = {"commit": "b" * 40, "ancestors": ["a" * 40, "f" * 40], "resolutions": []}
    ctx.path.write_text(json.dumps({**data, "unreviewed": [entry]}))


def confirm(ctx, merge_commit="b" * 40):
    gap = ctx.state.get("gaps", "gap-1")
    gap["confirmation"] = {
        "merge_commit": merge_commit,
        "principal_id": "admin-1",
        "email": "admin@example.com",
        "at": 50,
    }
    ctx.state.put("gaps", "gap-1", gap)


def test_unreviewed_fix_is_recorded_on_the_gap_and_does_not_resolve(resolution):
    c = resolution
    write_unreviewed(c)
    c.commit[0] = c.built[0] = "b" * 40
    c.model.reload()
    c.service.reconcile()
    gap = c.state.get("gaps", "gap-1")
    assert gap["status"] == "open" and gap["delivery_status"] == UNREVIEWED
    assert gap["pull_request"] == "https://github.com/example/project/pull/1"
    assert gap["unreviewed_fix"] == {
        "pull_request": "https://github.com/example/project/pull/1",
        "merge_commit": "b" * 40,
        "author": "author",
        "merged_at": "2026-10-20T10:00:00Z",
    }
    assert "verification" not in gap and not c.state.scan("gap_notices")
    assert not c.model.current.metrics["revenue"].get("verified")


def test_confirmed_fix_waits_for_build_then_resolves_with_the_admin_on_record(resolution):
    c = resolution
    write_unreviewed(c)
    c.service.reconcile()
    confirm(c)
    c.commit[0] = "b" * 40
    c.model.reload()
    c.service.reconcile()
    assert c.state.get("gaps", "gap-1")["delivery_status"] == "merged, waiting for build"
    c.built[0] = "b" * 40
    c.model.reload()
    c.service.reconcile()
    gap = c.state.get("gaps", "gap-1")
    assert gap["status"] == "resolved" and gap["resolved_commit"] == "b" * 40
    assert gap["verification"] == {
        "pull_request": "https://github.com/example/project/pull/1",
        "merge_commit": "b" * 40,
        "author": "author",
        "reviewers": [],
        "confirmed_by": "admin@example.com",
        "merged_at": "2026-10-20T10:00:00Z",
    }
    assert c.model.current.metrics["revenue"]["verified"]["confirmed_by"] == "admin@example.com"
    assert [n["principal_id"] for _, n in c.state.scan("gap_notices")] == ["p1"]


def test_confirmation_of_an_earlier_merge_does_not_cover_a_later_one(resolution):
    c = resolution
    write_unreviewed(c, merge_commit="f" * 40)
    c.service.reconcile()
    confirm(c, merge_commit="f" * 40)
    write_unreviewed(c, merge_commit="b" * 40)
    c.commit[0] = c.built[0] = "b" * 40
    c.model.reload()
    c.service.reconcile()
    gap = c.state.get("gaps", "gap-1")
    assert gap["status"] == "open" and gap["delivery_status"] == UNREVIEWED
    assert gap["unreviewed_fix"]["merge_commit"] == "b" * 40


@pytest.mark.parametrize(
    "change",
    [
        {"merge_commit": "e" * 40},
        {"pull_request": "https://evil.example/pull/1"},
        {"author": ""},
        {"metrics": []},
        {"gap": ""},
    ],
)
def test_invalid_unreviewed_entry_changes_nothing(resolution, change):
    c = resolution
    write_unreviewed(c, **change)
    c.service.reconcile()
    gap = c.state.get("gaps", "gap-1")
    assert "unreviewed_fix" not in gap and "delivery_status" not in gap


def test_unreviewed_entry_does_not_reopen_a_resolved_gap(resolution):
    c = resolution
    gap = c.state.get("gaps", "gap-1")
    c.state.put("gaps", "gap-1", {**gap, "status": "resolved", "delivery_status": "live"})
    write_unreviewed(c)
    c.service.reconcile()
    gap = c.state.get("gaps", "gap-1")
    assert gap["status"] == "resolved" and gap["delivery_status"] == "live"
    assert "unreviewed_fix" not in gap
