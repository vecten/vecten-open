import errno
import stat
from pathlib import Path

import pytest

from vecten_shared.config import VectenConfig, save_config, stack_project_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec, Provider, get_provider
from vecten_shared.provider.local import LocalProvider


def test_get_provider_lists_the_three_names(tmp_path):
    with pytest.raises(Failure) as exc:
        get_provider("gcp", tmp_path)
    assert exc.value.code == ExitCode.USAGE
    assert (
        "aws" in exc.value.message
        and "scaleway" in exc.value.message
        and "local" in exc.value.message
    )
    assert isinstance(get_provider("local", tmp_path), LocalProvider)


def test_secrets_from_dotenv(tmp_path):
    (tmp_path / ".env").write_text("HUBSPOT_API_KEY=abc\n")
    p = LocalProvider(tmp_path, inside_compose=False)
    assert p.get_secret("HUBSPOT_API_KEY") == "abc"
    p.put_secret("STRIPE_KEY", "xyz")
    p.put_secret("HUBSPOT_API_KEY", "def")
    text = (tmp_path / ".env").read_text()
    assert (
        "STRIPE_KEY=xyz" in text
        and "HUBSPOT_API_KEY=def" in text
        and text.count("HUBSPOT_API_KEY") == 1
    )


def test_put_secret_keeps_other_lines_and_comments(tmp_path):
    (tmp_path / ".env").write_text("# local secrets\nA=1\nexport B=2\nC=3")
    p = LocalProvider(tmp_path, inside_compose=False)
    p.put_secret("B", "two")
    p.put_secret("D", "4")
    assert (tmp_path / ".env").read_text() == "# local secrets\nA=1\nB=two\nC=3\nD=4\n"
    assert p.get_secret("B") == "two"


def test_fresh_env_file_is_private(tmp_path):
    LocalProvider(tmp_path).put_secret("STRIPE_KEY", "xyz")
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_missing_secret_names_secret_set(tmp_path):
    with pytest.raises(Failure) as exc:
        LocalProvider(tmp_path, inside_compose=False).get_secret("NOPE")
    assert exc.value.code == ExitCode.PREREQUISITE and "secret set NOPE" in exc.value.message


def test_connections_point_at_the_local_postgres(tmp_path):
    p = LocalProvider(tmp_path)
    w, a, m = (p.connection("local", db) for db in ("warehouse", "airflow", "mcp_state"))
    assert all(
        isinstance(c, ConnectionSpec) and c.kind == "postgres" and c.host == "postgres"
        for c in (w, a, m)
    )
    assert (w.database, w.role) == ("warehouse", "loader")
    assert (a.database, a.role) == ("airflow", "airflow")
    assert (m.database, m.role) == ("mcp_state", "mcp_state")
    with pytest.raises(Failure) as exc:
        p.connection("local", "other")
    assert "warehouse, airflow, mcp_state" in exc.value.message


def test_host_view_uses_published_port(tmp_path):
    c = LocalProvider(tmp_path, inside_compose=False).connection("local", "warehouse")
    assert (c.host, c.port) == ("localhost", 55432)


def test_noops(tmp_path):
    p = LocalProvider(tmp_path)
    assert p.provision("local") == {}
    p.bootstrap("local", "acme/data")
    p.destroy("local")
    p.ship("local", "platform", tmp_path)


@pytest.mark.parametrize("value", ["p${HOME}x", "a$b", "x # y", "  pad  ", 'q"q', "C:\\path"])
def test_values_round_trip_literally(tmp_path, value):
    p = LocalProvider(tmp_path, inside_compose=False)
    p.put_secret("S", value)
    assert p.get_secret("S") == value


@pytest.mark.parametrize("value", ["it's", "a\nX=1"])
def test_unstorable_values_are_refused(tmp_path, value):
    p = LocalProvider(tmp_path)
    with pytest.raises(Failure) as exc:
        p.put_secret("S", value)
    assert exc.value.code == ExitCode.USAGE
    assert "secret S contains a character" in exc.value.message


@pytest.mark.parametrize("value", ["a\\\\b", "a b\\"])
def test_backslash_sequences_dotenv_would_unescape_are_refused(tmp_path, value):
    with pytest.raises(Failure) as exc:
        LocalProvider(tmp_path).put_secret("S", value)
    assert exc.value.code == ExitCode.USAGE and "backslash" in exc.value.message


def test_refused_newline_value_injects_nothing(tmp_path):
    p = LocalProvider(tmp_path, inside_compose=False)
    p.put_secret("A", "1")
    before = (tmp_path / ".env").read_text()
    with pytest.raises(Failure):
        p.put_secret("S", "a\nX=1")
    assert (tmp_path / ".env").read_text() == before
    with pytest.raises(Failure) as exc:
        p.get_secret("X")
    assert exc.value.code == ExitCode.PREREQUISITE


@pytest.mark.parametrize("name", ["BAD NAME", "A=B"])
def test_bad_names_are_refused(tmp_path, name):
    with pytest.raises(Failure) as exc:
        LocalProvider(tmp_path).put_secret(name, "v")
    assert exc.value.code == ExitCode.USAGE and "[A-Za-z_][A-Za-z0-9_]*" in exc.value.message
    assert not (tmp_path / ".env").exists()


def test_empty_secret_is_reported_as_empty(tmp_path):
    (tmp_path / ".env").write_text("E=\n")
    with pytest.raises(Failure) as exc:
        LocalProvider(tmp_path, inside_compose=False).get_secret("E")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert exc.value.message == "secret E is empty in .env; run `vecten secret set E`"


def test_missing_secret_message(tmp_path):
    (tmp_path / ".env").write_text("E=1\n")
    with pytest.raises(Failure) as exc:
        LocalProvider(tmp_path, inside_compose=False).get_secret("M")
    assert exc.value.message == "secret M is not set; run `vecten secret set M`"


def test_put_secret_tightens_a_loose_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    env.chmod(0o644)
    LocalProvider(tmp_path).put_secret("B", "2")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_log_volume_is_named_after_the_compose_project(tmp_path):
    save_config(
        tmp_path,
        VectenConfig.model_validate(
            {"users": 3, "project": {"name": "acme", "scaffold_version": "0.1.0"}}
        ),
    )
    target = LocalProvider(tmp_path).log_target("local")
    assert target.kind == "docker_volume"
    assert target.location == f"{stack_project_name(tmp_path, 'acme')}_airflow-logs"
    assert target.location.startswith("vecten-acme-")


def test_inside_compose_reads_secrets_from_the_environment(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("HUBSPOT_API_KEY=from-file\n")
    monkeypatch.setenv("HUBSPOT_API_KEY", "from-env")
    in_stack = LocalProvider(tmp_path, inside_compose=True)
    on_host = LocalProvider(tmp_path, inside_compose=False)
    assert in_stack.get_secret("HUBSPOT_API_KEY") == "from-env"
    assert on_host.get_secret("HUBSPOT_API_KEY") == "from-file"


def test_inside_compose_missing_secret_names_secret_set(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(Failure) as exc:
        LocalProvider(tmp_path, inside_compose=True).get_secret("NOPE_KEY")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "vecten secret set NOPE_KEY" in exc.value.message


def test_inside_compose_empty_secret_names_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPTY_KEY", "")
    with pytest.raises(Failure) as exc:
        LocalProvider(tmp_path, inside_compose=True).get_secret("EMPTY_KEY")
    assert exc.value.message == (
        "secret EMPTY_KEY is empty in the environment; run `vecten secret set EMPTY_KEY`"
    )


def test_list_secrets_returns_names_only_sorted(tmp_path):
    provider = LocalProvider(tmp_path, inside_compose=False)
    provider.put_secret("B_KEY", "2")
    provider.put_secret("A_KEY", "1")
    assert provider.list_secrets() == ["A_KEY", "B_KEY"]
    assert LocalProvider(tmp_path / "other", inside_compose=False).list_secrets() == []


def test_local_provider_satisfies_the_protocol(tmp_path):
    provider: Provider = LocalProvider(tmp_path)
    assert callable(provider.list_secrets)


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


def test_an_unreadable_env_stops_get_and_list(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("A=1\n")
    deny_env(monkeypatch)
    provider = LocalProvider(tmp_path, inside_compose=False)
    for call in (lambda: provider.get_secret("A"), provider.list_secrets):
        with pytest.raises(Failure) as raised:
            call()
        assert raised.value.code == ExitCode.PREREQUISITE
        assert ".env" in raised.value.message


def test_put_secret_leaves_an_unreadable_env_unchanged(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("A=1\n")
    with monkeypatch.context() as denied:
        deny_env(denied)
        with pytest.raises(Failure) as raised:
            LocalProvider(tmp_path).put_secret("B", "2")
    assert raised.value.code == ExitCode.PREREQUISITE
    assert (tmp_path / ".env").read_text() == "A=1\n"
