import errno
import os
from pathlib import Path

import pytest

from vecten_shared.envfile import read_env_text, read_env_values
from vecten_shared.output import ExitCode, Failure


def deny_env(monkeypatch):
    real_read, real_is_file = Path.read_text, Path.is_file

    def read_text(self, *args, **kwargs):
        if self.name == ".env":
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(
        Path, "is_file", lambda self: False if self.name == ".env" else real_is_file(self)
    )


def test_a_missing_file_holds_no_secrets(tmp_path):
    assert read_env_text(tmp_path / ".env") is None
    assert read_env_values(tmp_path / ".env") == {}


def test_values_read_literally(tmp_path):
    (tmp_path / ".env").write_text("A=1\nB='x$y'\nC\n")
    assert read_env_values(tmp_path / ".env") == {"A": "1", "B": "x$y", "C": None}


def test_a_sandbox_denial_exits_prerequisite(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("A=1\n")
    deny_env(monkeypatch)
    for read in (read_env_text, read_env_values):
        with pytest.raises(Failure) as raised:
            read(tmp_path / ".env")
        assert raised.value.code == ExitCode.PREREQUISITE
        assert str(tmp_path / ".env") in raised.value.message
        assert "Operation not permitted" in raised.value.message


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_file_exits_prerequisite(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    env.chmod(0)
    try:
        with pytest.raises(Failure) as raised:
            read_env_text(env)
    finally:
        env.chmod(0o600)
    assert raised.value.code == ExitCode.PREREQUISITE
    assert "Permission denied" in raised.value.message


def test_a_directory_exits_prerequisite(tmp_path):
    (tmp_path / ".env").mkdir()
    with pytest.raises(Failure) as raised:
        read_env_text(tmp_path / ".env")
    assert raised.value.code == ExitCode.PREREQUISITE
    assert "Is a directory" in raised.value.message
