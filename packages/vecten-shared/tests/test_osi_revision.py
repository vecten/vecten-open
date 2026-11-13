import json

import pytest

from vecten_shared.osi.revision import (
    build_covers,
    content_revision,
    read_build_finished,
    read_built_commit,
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
