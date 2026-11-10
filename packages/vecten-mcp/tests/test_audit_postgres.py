"""Exercise audit permissions and upgrades against a disposable PostgreSQL server."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from vecten_shared.sql import setup_sql_path

spec = importlib.util.spec_from_file_location(
    "audit_pg_util", Path(__file__).parents[2] / "vecten-shared/tests/docker_util.py"
)
util = importlib.util.module_from_spec(spec)
spec.loader.exec_module(util)
# The module's tests share one Postgres container and run in order against it, so a CI shard
# takes the module whole.
pytestmark = [pytest.mark.docker, pytest.mark.xdist_group("mcp-audit-postgres")]


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    if not util.docker_available():
        pytest.skip("Docker unavailable")
    init = tmp_path_factory.mktemp("audit-pg") / "init"
    init.mkdir(mode=0o755)
    shutil.copy(setup_sql_path(), init / "setup.resource")
    with util.pg_container(init, {"POSTGRES_PASSWORD": "fixture-master"}) as (connect, name):
        with connect("postgres", "fixture-master", "postgres") as conn:
            conn.autocommit = True
            conn.execute("CREATE ROLE mcp_state LOGIN PASSWORD 'fixture-state'")
            conn.execute("CREATE DATABASE mcp_state OWNER mcp_state")
        with connect("mcp_state", "fixture-state", "mcp_state") as conn:
            conn.execute(
                "CREATE TABLE access_events (id text PRIMARY KEY, at double precision NOT NULL, "
                "principal_id text, operation text NOT NULL, subject text, outcome text NOT NULL)"
            )
            conn.execute(
                "INSERT INTO access_events VALUES "
                "('legacy', extract(epoch FROM now()), 'admin', 'disable', 'user', 'allowed')"
            )
        for _ in range(2):
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-e",
                    "MCP_STATE_DB_PASSWORD=fixture-state",
                    name,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-v",
                    "with_airflow=false",
                    "-v",
                    "with_mcp_state=true",
                    "-v",
                    "with_warehouse=false",
                    "-v",
                    "with_mcp=false",
                    "-f",
                    "/docker-entrypoint-initdb.d/setup.resource",
                ],
                check=True,
                capture_output=True,
            )
        yield connect


def test_upgrade_preserves_access_events_and_runtime_is_append_only(database):
    with database("mcp_state", "fixture-state", "mcp_state") as conn:
        conn.autocommit = True
        assert conn.execute(
            "SELECT operation, subject FROM access_events WHERE id='legacy'"
        ).fetchone() == ("disable", "user")
        conn.execute("INSERT INTO audit_log VALUES ('receipt', extract(epoch FROM now()), '{}')")
        conn.execute(
            "INSERT INTO access_events VALUES "
            "('login', extract(epoch FROM now()), 'user', 'login', 'client', 'allowed')"
        )
        assert conn.execute("SELECT id FROM audit_log WHERE id='receipt'").fetchone() == (
            "receipt",
        )
        partitions = [
            row[0]
            for row in conn.execute(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                "WHERE i.inhparent IN ('audit_log'::regclass, 'access_events'::regclass)"
            )
        ]
        for table in ["audit_log", "access_events", *partitions]:
            for statement in ("UPDATE {} SET id='changed'", "DELETE FROM {}", "TRUNCATE {}"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql.SQL(statement).format(sql.Identifier(table)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SET ROLE mcp_state_maintenance")
        assert conn.execute("SELECT current_user").fetchone() == ("mcp_state",)


def test_default_retention_drops_expired_partitions_and_default_rows(database):
    with database("postgres", "fixture-master", "mcp_state") as admin:
        day = admin.execute("SELECT floor(extract(epoch FROM now()) / 86400)::bigint").fetchone()[0]
        old_day = day - 100
        partition = f"audit_log_day_{old_day}"
        admin.execute(
            sql.SQL("CREATE TABLE {} PARTITION OF audit_log FOR VALUES FROM ({}) TO ({})").format(
                sql.Identifier(partition),
                sql.Literal(old_day * 86400),
                sql.Literal((old_day + 1) * 86400),
            )
        )
        admin.execute(
            sql.SQL("ALTER TABLE {} OWNER TO mcp_state_maintenance").format(
                sql.Identifier(partition)
            )
        )
        for name, at in [
            ("old-partition", old_day * 86400),
            ("old-default", (day - 200) * 86400),
            ("recent", day * 86400),
        ]:
            admin.execute("INSERT INTO audit_log VALUES (%s, %s, '{}')", (name, at))
            admin.execute(
                "INSERT INTO access_events VALUES (%s, %s, 'user', 'login', 'client', 'allowed')",
                (name, at),
            )
    with database("mcp_state", "fixture-state", "mcp_state") as runtime:
        runtime.execute("SELECT vecten_audit.maintain()")
        assert runtime.execute("SELECT to_regclass(%s)", (partition,)).fetchone() == (None,)
        for table in ("audit_log", "access_events"):
            ids = {
                row[0]
                for row in runtime.execute(
                    sql.SQL("SELECT id FROM {}").format(sql.Identifier(table))
                )
            }
            assert "recent" in ids
            assert not {"old-partition", "old-default"} & ids
        assert runtime.execute("SELECT current_user").fetchone() == ("mcp_state",)
        assert runtime.execute(
            "SELECT has_table_privilege(current_user, 'audit_log', 'DELETE')"
        ).fetchone() == (False,)


def test_usage_aggregates_json_fields_in_postgres(database):
    import time
    from types import SimpleNamespace

    from sqlalchemy import create_engine

    from vecten_mcp.audit import AuditService, receipt

    engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: database("mcp_state", "fixture-state", "mcp_state"),
    )
    audit = AuditService(SimpleNamespace(transaction=engine.begin), start=False)
    now = time.time() + 10
    try:
        for tool, outcome, reason in [
            ("query_metrics", "compiled", None),
            ("sql-exec", "refused", "statement_policy"),
            ("describe", "compiled", None),
        ]:
            audit.write(
                {
                    "id": receipt(None)["id"],
                    "at": now,
                    "tool": tool,
                    "principal_id": "usage-person",
                    "resolution": outcome,
                    "refusal_reason": reason,
                }
            )
        assert audit.usage(since=now, until=now) == {
            "questions": 2,
            "by_tool": {"query_metrics": 1, "sql-exec": 1},
            "by_principal": {"usage-person": 2},
            "refusals": {"statement_policy": 1},
        }
    finally:
        audit.close()
        engine.dispose()


def test_projection_browse_and_chain_read_json_fields_in_postgres(database):
    import time
    from types import SimpleNamespace

    from sqlalchemy import create_engine

    from vecten_mcp.audit import AuditService, receipt

    engine = create_engine(
        "postgresql+psycopg://",
        creator=lambda: database("mcp_state", "fixture-state", "mcp_state"),
    )
    audit = AuditService(SimpleNamespace(transaction=engine.begin), start=False)
    now = time.time() + 20
    refused, follow = receipt(None)["id"], receipt(None)["id"]
    try:
        audit.write(
            {
                "id": refused,
                "at": now,
                "tool": "query_metrics",
                "principal_id": "projection-person",
                "resolution": "refused",
                "refusal_reason": "unknown_metric",
                "metrics": ["revenue"],
                "datasets": None,
                "columns": ["orders.amount"],
                "request_chain_id": refused,
                "sql": "SELECT secret",
            }
        )
        audit.write(
            {
                "id": follow,
                "at": now + 1,
                "tool": "sql-exec",
                "principal_id": "projection-person",
                "resolution": "agent_sql",
                "request_chain_id": refused,
            }
        )
        first, second = audit.projection(since=now, until=now + 1)
        assert first["metrics"] == ["revenue"] and first["datasets"] == []
        assert first["columns"] == ["orders.amount"] and "sql" not in first
        assert second["metrics"] == [] and second["refusal_reason"] is None
        assert [r["id"] for r in audit.browse(principal_id="projection-person")] == [
            follow,
            refused,
        ]
        assert [r["id"] for r in audit.chain_rows(refused)] == [refused, follow]
    finally:
        audit.close()
        engine.dispose()


def test_feedback_folding_is_atomic_with_concurrent_postgres_workers(database):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import create_engine

    from vecten_mcp.audit import AuditService, receipt
    from vecten_mcp.feedback import FeedbackService
    from vecten_mcp.state import StateStore, metadata, records

    state = StateStore.__new__(StateStore)
    state.engine = create_engine(
        "postgresql+psycopg://", creator=lambda: database("mcp_state", "fixture-state", "mcp_state")
    )
    metadata.create_all(state.engine, tables=[records])
    audit = AuditService(state, start=False)
    metric = "missing_" + uuid4().hex
    runtime = SimpleNamespace(state=state, audit=audit)
    try:
        for _ in range(12):
            audit.write(
                {
                    "id": receipt(None)["id"],
                    "at": time.time(),
                    "tool": "query_metrics",
                    "resolution": "refused",
                    "refusal_reason": "unknown_metric",
                    "arguments": {"metrics": [metric]},
                    "principal_id": "fixture-person",
                }
            )
        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(lambda _: FeedbackService(runtime).fold(), range(2)))
        gaps = [g for _, g in state.scan("gaps") if g["names"] == [metric]]
        assert len(gaps) == 1 and gaps[0]["occurrence_count"] == 12
        FeedbackService(runtime).fold()
        assert [g for _, g in state.scan("gaps") if g["names"] == [metric]][0][
            "occurrence_count"
        ] == 12
    finally:
        audit.close()
        state.close()


@pytest.mark.parametrize("fallback", [False, True])
def test_folding_preserves_concurrent_admin_status(database, monkeypatch, fallback):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread
    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import create_engine

    from vecten_mcp.audit import AuditService
    from vecten_mcp.feedback import FeedbackService
    from vecten_mcp.state import StateStore, metadata, records
    from vecten_mcp.tool_service import ToolService

    state = StateStore.__new__(StateStore)
    state.engine = create_engine(
        "postgresql+psycopg://", creator=lambda: database("mcp_state", "fixture-state", "mcp_state")
    )
    metadata.create_all(state.engine, tables=[records])
    audit = AuditService(state, start=False)
    runtime = SimpleNamespace(state=state)
    service = FeedbackService(runtime)
    uid = uuid4().hex
    refusal = {
        "id": uid,
        "at": time.time(),
        "tool": "query_metrics",
        "resolution": "refused",
        "refusal_reason": "unknown_metric",
        "arguments": {"metrics": [uid]},
        "principal_id": uid,
        "session_id": uid,
    }
    audit.write(refusal)
    service.fold()
    gap_id = next(k for k, g in state.scan("gaps") if g["names"] == [uid])
    audit.write(
        {
            **refusal,
            "id": uid + "next",
            **(
                {
                    "tool": "sql-exec",
                    "resolution": "agent_sql",
                    "request_chain_id": uid,
                    "sql": "SELECT 1",
                }
                if fallback
                else {}
            ),
        }
    )
    read, release, editing = Event(), Event(), Event()
    original = state.get

    def pause_fold(kind, key, **kwargs):
        value = original(kind, key, **kwargs)
        if kind == "gaps" and key == gap_id and current_thread().name.startswith("fold"):
            read.set()
            assert release.wait(10)
        return value

    def edit():
        editing.set()
        ToolService(runtime).set_gap_status(
            SimpleNamespace(principal={"kind": "human", "admin": True}), gap_id, "dismissed"
        )

    monkeypatch.setattr(state, "get", pause_fold)
    try:
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="fold") as folding,
            ThreadPoolExecutor(max_workers=1) as admin,
        ):
            future = folding.submit(service.fold)
            assert read.wait(10)
            update = admin.submit(edit)
            assert editing.wait(10)
            # A lock-free fold lets this update commit before overwriting it.
            try:
                update.result(timeout=0.3)
            except TimeoutError:
                pass
            finally:
                release.set()
            future.result(timeout=10)
            update.result(timeout=10)
        assert state.get("gaps", gap_id)["status"] == "dismissed"
    finally:
        release.set()
        audit.close()
        state.close()
