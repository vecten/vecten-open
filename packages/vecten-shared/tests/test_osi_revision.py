import json

import pytest

from vecten_shared.osi.revision import (
    build_covers,
    content_revision,
    read_build_finished,
    read_built_commit,
    read_built_relations,
)


def test_local_revision_tracks_names_and_contents_but_not_absolute_path(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    for path in (one, two):
        path.mkdir()
        (path / "model.json").write_text("{}")
    assert content_revision(one) == content_revision(two)
    (two / "model.json").write_text("{ }")
    assert content_revision(one) != content_revision(two)


@pytest.mark.parametrize(
    "marker,expected",
    [
        ("abc\n", True),
        ("def", False),
        (None, False),
        ("not-json", False),
        (json.dumps({"commit": "def", "ancestors": ["abc"]}), True),
        (json.dumps({"commit": "def", "ancestors": "abc"}), False),
    ],
)
def test_build_evidence_never_guesses_order_from_commit_names(marker, expected):
    assert build_covers(marker, "abc") is expected


def test_read_local_build_marker(tmp_path):
    env = {"VECTEN_ARTIFACT_DIR": str(tmp_path)}
    assert read_built_commit(env) is None
    (tmp_path / "dbt").mkdir()
    (tmp_path / "dbt/built_commit").write_text("abc\n")
    assert read_built_commit(env) == "abc\n"


def test_read_local_build_finish_time(tmp_path):
    env = {"VECTEN_ARTIFACT_DIR": str(tmp_path)}
    assert read_build_finished(env) is None
    (tmp_path / "dbt").mkdir()
    (tmp_path / "dbt/build_finished_at").write_text("2026-11-12T10:00:00+00:00\n")
    assert read_build_finished(env) == "2026-11-12T10:00:00+00:00\n"


RELATIONS = [{"database": "w", "schema": "marts", "name": "orders", "kind": "table"}]


def record(tmp_path, finished_at, finished):
    (tmp_path / "dbt").mkdir(exist_ok=True)
    body = {"finished_at": finished_at, "relations": RELATIONS}
    (tmp_path / "dbt/built_relations.json").write_text(json.dumps(body) + "\n")
    if finished is not None:
        (tmp_path / "dbt/build_finished_at").write_text(finished + "\n")


def test_built_relations_are_read_while_no_later_build_finished(tmp_path):
    env = {"VECTEN_ARTIFACT_DIR": str(tmp_path)}
    assert read_built_relations(env) is None
    record(tmp_path, "2026-11-17T09:00:00+00:00", "2026-11-17T09:00:00+00:00")
    assert read_built_relations(env) == RELATIONS


@pytest.mark.parametrize("finished", ["2026-11-17T10:00:00+00:00", None])
def test_built_relations_a_later_or_unrecorded_finish_may_postdate_are_not_read(tmp_path, finished):
    record(tmp_path, "2026-11-17T09:00:00+00:00", finished)
    assert read_built_relations({"VECTEN_ARTIFACT_DIR": str(tmp_path)}) is None
