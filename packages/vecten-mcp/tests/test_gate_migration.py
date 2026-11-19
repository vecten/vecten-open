"""The step to schema version 2 is what adds gate_sessions to an existing database."""

from sqlalchemy import MetaData, create_engine, inspect, select

import vecten_mcp.state as state_module
from vecten_mcp.state import StateStore, schema_version


def _version_one(url):
    """A database as the release before the gate left it: its tables, version 1, no gate."""
    old = MetaData()
    for table in state_module.metadata.sorted_tables:
        if table.name != "gate_sessions":
            table.to_metadata(old)
    engine = create_engine(url)
    old.create_all(engine)
    with engine.begin() as conn:
        conn.execute(old.tables["schema_version"].insert().values(version=1))
    engine.dispose()


def test_opening_a_version_one_database_runs_the_step_that_adds_the_gate(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'state.db'}"
    _version_one(url)
    ran = []
    step = state_module.MIGRATIONS[2]
    monkeypatch.setitem(state_module.MIGRATIONS, 2, lambda conn: (ran.append(2), step(conn)))
    store = StateStore(url)
    assert ran == [2]
    assert "gate_sessions" in inspect(store.engine).get_table_names()
    with store.transaction() as conn:
        assert conn.execute(select(schema_version.c.version)).scalar_one() == 2
    store.close()


def test_without_the_step_an_old_database_gets_no_gate_table(tmp_path, monkeypatch):
    # The step, not the creation of missing tables at start, is what brings the table.
    url = f"sqlite:///{tmp_path / 'state.db'}"
    _version_one(url)
    monkeypatch.setitem(state_module.MIGRATIONS, 2, lambda conn: None)
    store = StateStore(url)
    assert "gate_sessions" not in inspect(store.engine).get_table_names()
    store.close()


def test_a_new_database_has_the_gate_table_without_any_step(monkeypatch):
    def refuse(conn):
        raise AssertionError("a new database runs no step")

    monkeypatch.setitem(state_module.MIGRATIONS, 2, refuse)
    store = StateStore("sqlite://")
    assert "gate_sessions" in inspect(store.engine).get_table_names()
