import socket

import pytest

from vecten_platform.sync.testing import replay_connector_test


def test_skips_without_fixtures_naming_record(tmp_path, monkeypatch):
    connector_dir = tmp_path / "connectors" / "hubspot"
    (connector_dir / "fixtures").mkdir(parents=True)
    with pytest.raises(pytest.skip.Exception) as exc:
        replay_connector_test(connector_dir, "hubspot", tmp_path / "work", monkeypatch)
    assert "vecten connector run hubspot --record" in str(exc.value)


def test_replays_fixtures_into_duckdb_and_counts_rows(tmp_path, monkeypatch):
    connector_dir = tmp_path / "connectors" / "hubspot"
    fixtures = connector_dir / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "companies.jsonl").write_text('{"id": 1}\n{"id": 2}\n')
    (fixtures / "deals.jsonl").write_text('{"id": 9, "amount": 3}\n')
    counts = replay_connector_test(connector_dir, "hubspot", tmp_path / "work", monkeypatch)
    assert counts == {"companies": 2, "deals": 1}


def test_network_is_blocked_during_replay(tmp_path, monkeypatch):
    connector_dir = tmp_path / "connectors" / "x"
    (connector_dir / "fixtures").mkdir(parents=True)
    (connector_dir / "fixtures" / "t.jsonl").write_text('{"id": 1}\n')
    replay_connector_test(connector_dir, "x", tmp_path / "work", monkeypatch)
    # monkeypatch is still active inside the test, so the guard is observable here.
    with pytest.raises(RuntimeError, match="network access is disabled"):
        socket.create_connection(("127.0.0.1", 9))


def _fixture(tmp_path, name: str, text: str):
    connector_dir = tmp_path / "connectors" / "hubspot"
    fixtures = connector_dir / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / name).write_text(text)
    return connector_dir


def test_nested_items_pass_and_child_tables_are_ignored(tmp_path, monkeypatch):
    connector_dir = _fixture(
        tmp_path, "companies.jsonl", '{"id": 1, "tags": [{"t": "a"}, {"t": "b"}]}\n'
    )
    counts = replay_connector_test(connector_dir, "hubspot", tmp_path / "work", monkeypatch)
    assert counts == {"companies": 1}


def test_a_fixture_name_is_normalised_like_its_table(tmp_path, monkeypatch):
    connector_dir = _fixture(tmp_path, "Contact-Lists.jsonl", '{"id": 1}\n{"id": 2}\n')
    counts = replay_connector_test(connector_dir, "hubspot", tmp_path / "work", monkeypatch)
    assert counts == {"contact_lists": 2}


def test_an_empty_fixture_is_a_failure_naming_the_file(tmp_path, monkeypatch):
    connector_dir = _fixture(tmp_path, "companies.jsonl", '{"id": 1}\n')
    (connector_dir / "fixtures" / "deals.jsonl").write_text("")
    with pytest.raises(AssertionError, match=r"deals\.jsonl"):
        replay_connector_test(connector_dir, "hubspot", tmp_path / "work", monkeypatch)
