"""Gate sessions in mcp_state: a table of their own, added to an existing database."""

from sqlalchemy import inspect, select, text

from vecten_mcp.state import SCHEMA_VERSION, StateStore, schema_version


def _version(store):
    with store.transaction() as conn:
        return conn.execute(select(schema_version.c.version)).scalar_one()


def test_a_database_from_before_the_gate_gains_its_table_and_version(tmp_path):
    url = f"sqlite:///{tmp_path / 'state.db'}"
    store = StateStore(url)
    with store.transaction() as conn:
        conn.execute(text("DROP TABLE gate_sessions"))
        conn.execute(text("UPDATE schema_version SET version = 1"))
    store.close()
    again = StateStore(url)
    assert "gate_sessions" in inspect(again.engine).get_table_names()
    assert SCHEMA_VERSION == 2 and _version(again) == 2
    again.close()


def test_a_new_database_starts_at_the_current_version():
    store = StateStore("sqlite://")
    assert _version(store) == SCHEMA_VERSION


def test_a_gate_session_is_found_by_its_key_until_it_expires():
    store = StateStore("sqlite://")
    ada = store.invite("ada@example.com", "Ada", "ada")
    store.open_gate_session("k1", ada["id"], "portal.example.com", now=100, expires_at=200)
    found = store.gate_session("k1", now=150)
    assert found["principal_id"] == ada["id"] and found["hostname"] == "portal.example.com"
    assert store.gate_session("k1", now=200) is None
    assert store.gate_session("other", now=150) is None
    store.prune(250)
    with store.transaction() as conn:
        assert conn.execute(text("SELECT count(*) FROM gate_sessions")).scalar_one() == 0


def test_ending_a_persons_gate_sessions_leaves_everyone_elses():
    store = StateStore("sqlite://")
    ada = store.invite("ada@example.com", "Ada", "ada")
    bob = store.invite("bob@example.com", "Bob", "bob")
    store.open_gate_session("a1", ada["id"], "portal.example.com", now=0, expires_at=900)
    store.open_gate_session("a2", ada["id"], "dash.example.com", now=0, expires_at=900)
    store.open_gate_session("b1", bob["id"], "portal.example.com", now=0, expires_at=900)
    store.end_gate_sessions(ada["id"])
    assert store.gate_session("a1", now=1) is None and store.gate_session("a2", now=1) is None
    assert store.gate_session("b1", now=1)
    store.end_gate_session("b1")
    assert store.gate_session("b1", now=1) is None


def test_deleting_a_principal_deletes_its_gate_sessions():
    store = StateStore("sqlite://")
    ada = store.invite("ada@example.com", "Ada", "ada")
    store.open_gate_session("a1", ada["id"], "portal.example.com", now=0, expires_at=900)
    with store.transaction() as conn:
        conn.execute(text("DELETE FROM principals WHERE id = :id"), {"id": ada["id"]})
    assert store.gate_session("a1", now=1) is None
