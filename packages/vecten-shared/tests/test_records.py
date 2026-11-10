import os
import stat

import pytest

from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import read_record, write_record, write_text_atomic


def test_write_then_read(tmp_path):
    assert read_record(tmp_path, "up") is None
    write_record(tmp_path, "up", {"profiles": ["platform", "mcp", "local"]})
    rec = read_record(tmp_path, "up")
    assert rec["profiles"] == ["platform", "mcp", "local"]
    assert "recorded_at" in rec
    assert (tmp_path / ".vecten" / "records" / "up.json").is_file()


def test_corrupt_record_is_a_config_error(tmp_path):
    write_record(tmp_path, "up", {})
    (tmp_path / ".vecten" / "records" / "up.json").write_text("{not json")
    with pytest.raises(Failure) as exc:
        read_record(tmp_path, "up")
    assert exc.value.code == ExitCode.CONFIG and "up.json" in exc.value.message


def test_record_write_leaves_no_temp_file(tmp_path):
    write_record(tmp_path, "up", {"a": 1})
    assert [p.name for p in (tmp_path / ".vecten" / "records").iterdir()] == ["up.json"]


def test_fresh_record_takes_umask_mode(tmp_path):
    previous = os.umask(0o022)
    try:
        path = write_record(tmp_path, "up", {})
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_rewrite_preserves_existing_mode(tmp_path):
    path = write_record(tmp_path, "up", {})
    path.chmod(0o600)
    previous = os.umask(0o022)
    try:
        write_record(tmp_path, "up", {"a": 1})
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_explicit_mode_overrides_existing_mode(tmp_path):
    path = tmp_path / "f"
    path.write_text("old")
    path.chmod(0o644)
    write_text_atomic(path, "new", mode=0o600)
    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
