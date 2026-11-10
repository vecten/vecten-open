import importlib.util
import sys
from pathlib import Path

import psycopg
import pytest

from vecten_mcp.warehouse.pool import PrincipalPools
from vecten_mcp.warehouse.postgres import PostgresAdapter

path = Path(__file__).parents[2] / "vecten-shared/tests/docker_util.py"
spec = importlib.util.spec_from_file_location("phase11_pg_util", path)
util = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = util
spec.loader.exec_module(util)
# The module's tests share one Postgres container and run in order against it, so a CI shard
# takes the module whole.
pytestmark = [pytest.mark.docker, pytest.mark.xdist_group("mcp-postgres-identity-docker")]


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    if not util.docker_available():
        pytest.skip("Docker unavailable")
    root = tmp_path_factory.mktemp("identity-pg") / "init"
    root.mkdir(mode=0o755)
    with util.pg_container(root, {"POSTGRES_PASSWORD": "fixture-master"}) as (connect, _):

        def conn(user="postgres", password="fixture-master"):
            c = connect(user, password, "postgres")
            c.autocommit = True
            return c

        with conn() as c:
            c.execute("CREATE ROLE provisioner LOGIN CREATEROLE PASSWORD 'fixture-provisioner'")
            c.execute("CREATE ROLE impersonator LOGIN NOINHERIT PASSWORD 'fixture-impersonator'")
            c.execute("CREATE ROLE analyst NOLOGIN")
            c.execute("GRANT analyst TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")
            c.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            c.execute("CREATE SCHEMA core")
            c.execute("CREATE TABLE core.allowed(value int)")
            c.execute("CREATE TABLE core.denied(value int)")
            c.execute("INSERT INTO core.allowed VALUES (7)")
            c.execute("GRANT USAGE ON SCHEMA core TO analyst, provisioner")
            c.execute("GRANT SELECT ON core.allowed TO analyst")
            c.execute("GRANT SELECT ON core.allowed TO provisioner WITH GRANT OPTION")
        yield conn


def adapter(conn):
    return PostgresAdapter(
        lambda: conn("provisioner", "fixture-provisioner"),
        lambda: conn("impersonator", "fixture-impersonator"),
    )


def test_source_metadata_check_does_not_require_source_privileges(warehouse):
    from sqlglot import exp, parse_one

    checker = adapter(warehouse)
    with warehouse() as conn:
        conn.execute("CREATE SCHEMA metadata_only")
        conn.execute('CREATE TABLE metadata_only."Mixed Case" (id int)')
    assert checker.source_exists(
        parse_one('SELECT * FROM metadata_only."Mixed Case"').find(exp.Table)
    )
    assert not checker.source_exists(
        parse_one("SELECT * FROM metadata_only.missing").find(exp.Table)
    )
    assert not checker.source_exists(parse_one("SELECT * FROM other.core.allowed").find(exp.Table))


def test_query_executor_enforces_read_only_even_with_write_grants(warehouse):
    import time
    from types import SimpleNamespace

    from vecten_mcp.audit import receipt
    from vecten_mcp.queries import QueryService

    control = adapter(warehouse)
    control.create_account("query_reader", "human")
    control.membership("analyst", "query_reader")
    with warehouse() as conn:
        conn.execute("GRANT INSERT ON core.allowed TO query_reader")
        conn.execute(
            "CREATE FUNCTION core.write_probe() RETURNS int LANGUAGE plpgsql AS $$ "
            "BEGIN INSERT INTO core.allowed VALUES (999); RETURN 999; END $$"
        )
    state = SimpleNamespace(
        get=lambda *args: None, group=lambda name: {"warehouse_role": "analyst"}
    )
    pools = PrincipalPools(control)
    service = QueryService(state, pools, kind="postgres")
    caller = SimpleNamespace(
        principal={
            "id": "query_reader",
            "warehouse_name": "query_reader",
            "enabled": True,
            "provisioned": True,
        },
        session_id="query_session",
        expires_at=time.time() + 300,
        groups={"default"},
    )
    try:
        result = service.execute(
            caller, "SELECT current_user, value FROM core.allowed", [], receipt("abc")
        )
        assert result["rows"] == [["query_reader", 7]]
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            service.execute(caller, "SELECT core.write_probe()", [], receipt("abc"))
        with pytest.raises(ValueError, match="configuration"):
            service.execute(
                caller, "SELECT set_config('role','postgres',false)", [], receipt("abc")
            )
        with warehouse() as conn:
            assert conn.execute("SELECT value FROM core.allowed").fetchall() == [(7,)]
    finally:
        pools.close()
        with warehouse() as conn:
            conn.execute("DROP FUNCTION core.write_probe()")
            conn.execute("REVOKE INSERT ON core.allowed FROM query_reader")
        control.drop_account("query_reader")


def test_real_impersonation_permissions_and_last_session_revocation(warehouse):
    a = adapter(warehouse)
    a.create_account("test_jane", "human")
    a.membership("analyst", "test_jane")
    a.check_scope({"test_jane"})
    with warehouse() as c:
        assert c.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname='test_jane'"
        ).fetchone() == (None,)
    p = dict(id="jane", warehouse_name="test_jane", enabled=True, provisioned=True)
    pools = PrincipalPools(a, clock=lambda: 1)
    with pools.acquire(p, "a", 100) as c:
        assert c.execute(
            "SELECT current_user, session_user, current_setting('application_name')"
        ).fetchone() == ("test_jane", "impersonator", "test_jane")
        assert c.execute("SELECT value FROM core.allowed").fetchone() == (7,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT value FROM core.denied")
    with pools.acquire(p, "b", 100):
        pass
    pools.revoke_session("a")
    assert "test_jane" in a.stale_memberships()
    pools.revoke_session("b")
    with warehouse("impersonator", "fixture-impersonator") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SET ROLE test_jane")
    a.open_membership("test_jane")
    a.cleanup_sessions()
    assert not a.stale_memberships()


def test_adoption_keeps_existing_password_and_requires_bounded_admin(warehouse):
    a = adapter(warehouse)
    with warehouse() as c:
        c.execute("CREATE ROLE adopted_jan LOGIN PASSWORD 'own-fixture-password'")
        before = c.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname='adopted_jan'"
        ).fetchone()
    account = a.inspect_account("adopted_jan")
    assert not account.can_adopt and "INHERIT FALSE, SET FALSE" in account.adoption_sql
    with warehouse() as c:
        c.execute(account.adoption_sql)
    assert a.inspect_account("adopted_jan").can_adopt
    a.open_membership("adopted_jan")
    a.close_membership("adopted_jan")
    with warehouse() as c:
        assert (
            c.execute("SELECT rolpassword FROM pg_authid WHERE rolname='adopted_jan'").fetchone()
            == before
        )


def test_scope_rejects_elevated_and_inheriting_roles(warehouse):
    a = adapter(warehouse)
    registry = {"test_jane", "adopted_jan"}
    with warehouse() as c:
        c.execute("ALTER ROLE provisioner CREATEDB")
    try:
        with pytest.raises(ValueError, match="CREATEDB"):
            a.check_scope(registry)
    finally:
        with warehouse() as c:
            c.execute("ALTER ROLE provisioner NOCREATEDB")
    with warehouse() as c:
        c.execute("GRANT analyst TO impersonator WITH INHERIT TRUE")
    try:
        with pytest.raises(ValueError, match="membership analyst"):
            a.check_scope(registry)
    finally:
        with warehouse() as c:
            c.execute("REVOKE analyst FROM impersonator")


def test_rotation_preserves_open_session_and_new_connections(warehouse):
    from vecten_mcp.rotation import PasswordRotation
    from vecten_mcp.state import StateStore

    class Store:
        value = "fixture-impersonator"

        def get_secret(self, name):
            return self.value

        def put_secret(self, name, value):
            self.value = value

    store = Store()
    current = warehouse("impersonator", store.value)
    try:
        rotation = PasswordRotation(
            StateStore("sqlite://"), store, lambda password: warehouse("impersonator", password)
        )
        rotation.rotate()
        assert current.execute("SELECT current_user").fetchone() == ("impersonator",)
        with warehouse("impersonator", store.value) as new:
            assert new.execute("SELECT current_user").fetchone() == ("impersonator",)
    finally:
        current.close()


def test_configured_runtime_serves_principal_query_against_postgres(tmp_path, monkeypatch):
    import shutil
    import subprocess
    import warnings
    from types import SimpleNamespace

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
        )
        from starlette.testclient import TestClient
    from vecten_mcp.app import create_app
    from vecten_mcp.extensions import McpExtensions
    from vecten_mcp.runtime_setup import configured_runtime
    from vecten_mcp.transport import current_request
    from vecten_shared.config import VectenConfig

    init = tmp_path / "init"
    init.mkdir(mode=0o755)
    from vecten_shared.sql import setup_sql_path

    shutil.copy(setup_sql_path(), init / "setup.resource")
    with util.pg_container(init, {"POSTGRES_PASSWORD": "fixture-master"}) as (connect, container):
        subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                "MCP_STATE_DB_PASSWORD=fixture-state",
                container,
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
        with connect("postgres", "fixture-master", "postgres") as c:
            c.autocommit = True
            c.execute("CREATE ROLE provisioner LOGIN CREATEROLE PASSWORD 'fixture-provisioner'")
            c.execute("CREATE ROLE impersonator LOGIN NOINHERIT PASSWORD 'fixture-impersonator'")
            c.execute("CREATE ROLE analyst NOLOGIN")
            c.execute("GRANT analyst TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")
            c.execute("GRANT CONNECT ON DATABASE postgres TO provisioner, impersonator")
            host, port = c.info.host, c.info.port

        class Provider:
            def connection(self, env, db):
                return SimpleNamespace(
                    host=host, port=port, database="mcp_state" if db == "mcp_state" else "postgres"
                )

        monkeypatch.setattr("vecten_mcp.runtime_setup.get_provider", lambda *args: Provider())
        for name, value in {
            "VECTEN_ENV": "local",
            "VECTEN_PROVIDER": "local",
            "VECTEN_MCP_PUBLIC_URL": "https://mcp.example",
            "VECTEN_MCP_SECRET_DIR": str(tmp_path / "keys"),
            "MCP_STATE_DB_PASSWORD": "fixture-state",
            "MCP_PROVISIONER_PASSWORD": "fixture-provisioner",
            "MCP_IMPERSONATOR_PASSWORD": "fixture-impersonator",
        }.items():
            monkeypatch.setenv(name, value)

        def who():
            runtime, caller = current_request.get()
            from vecten_mcp.audit import receipt

            return runtime.queries.execute(caller, "SELECT current_user", [], receipt(None))

        extensions = McpExtensions({"who": {"handler": who, "groups": ["default"]}}, {}, {}, {})
        config = VectenConfig(users=5, project={"name": "demo", "scaffold_version": "0.1.0"})
        runtime = configured_runtime(tmp_path, config, extensions)
        with connect("postgres", "fixture-master", "mcp_state") as c:
            c.execute("GRANT UPDATE ON audit_log TO mcp_state")
            c.commit()
            with pytest.raises(ValueError, match="append-only"):
                runtime.state.check_audit_scope()
            c.execute("REVOKE UPDATE ON audit_log FROM mcp_state")
            c.commit()
            c.execute("GRANT DELETE ON audit_log_default TO mcp_state")
            c.commit()
            with pytest.raises(ValueError, match="partitions"):
                runtime.state.check_audit_scope()
            c.execute("REVOKE DELETE ON audit_log_default FROM mcp_state")
            c.commit()
        runtime.state.check_audit_scope()
        service = runtime.administration.create_service("runtime_test")
        creds = service["credentials"]
        token = runtime.oauth.client_credentials(
            creds["client_id"], creds["client_secret"], runtime.oauth.resource
        )["access_token"]
        with TestClient(create_app(runtime=runtime), base_url="https://mcp.example") as http:
            result = http.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2026-07-28",
                    "mcp-method": "tools/call",
                    "mcp-name": "who",
                    "Authorization": "Bearer " + token,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "who",
                        "arguments": {},
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/clientCapabilities": {},
                        },
                    },
                },
            )
            assert result.status_code == 200, result.text
            assert "svc_runtime_test" in result.text
            stamp = result.json()["result"]["_meta"]["vecten/receipt"]
            runtime.audit.flush()
            row = runtime.audit.find(stamp["id"])
            assert row["principal_kind"] == "service"
            assert row["row_count"] == 1
            assert row["sql_sha256"] == stamp["sql_sha256"]
            assert row["sql_sha256"] is not None
            assert "CURRENT_USER" in row["sql"]


def test_scope_rejects_database_create_and_empty_schema_ownership(warehouse):
    a = adapter(warehouse)
    registry = {"test_jane", "adopted_jan"}
    with warehouse() as conn:
        conn.execute("GRANT CREATE ON DATABASE postgres TO impersonator")
    try:
        with pytest.raises(ValueError, match="database.*CREATE"):
            a.check_scope(registry)
    finally:
        with warehouse() as conn:
            conn.execute("REVOKE CREATE ON DATABASE postgres FROM impersonator")
    with warehouse() as conn:
        conn.execute("CREATE SCHEMA unexpected AUTHORIZATION impersonator")
    try:
        with pytest.raises(ValueError, match="schema.*unexpected"):
            a.check_scope(registry)
    finally:
        with warehouse() as conn:
            conn.execute("DROP SCHEMA unexpected")


def test_existing_warehouse_script_and_catalog_draft(tmp_path, monkeypatch):
    import json
    import subprocess
    from types import SimpleNamespace

    from vecten.mcp_onboarding import catalog_rows
    from vecten_shared.config import VectenConfig
    from vecten_shared.identity_setup import introspection_document, provisioner_script
    from vecten_shared.osi.validate import validate_osi

    init = tmp_path / "init"
    init.mkdir(mode=0o755)
    with util.pg_container(init, {"POSTGRES_PASSWORD": "fixture-master"}) as (connect, name):
        with connect("postgres", "fixture-master", "postgres") as conn:
            conn.autocommit = True
            conn.execute("CREATE SCHEMA core")
            conn.execute("CREATE TABLE core.orders (id integer, amount numeric)")
            host, port = conn.info.host, conn.info.port
        script = provisioner_script("postgres", database="postgres", issuer="https://mcp.example")
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                "MCP_PROVISIONER_PASSWORD=fixture-provisioner",
                "-e",
                "MCP_IMPERSONATOR_PASSWORD=fixture-impersonator",
                name,
                "psql",
                "-U",
                "postgres",
                "-d",
                "postgres",
            ],
            input=script,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        config = VectenConfig(
            users=5,
            project={"name": "demo", "scaffold_version": "0.1.0"},
            components=["mcp"],
            warehouse={"existing": True, "host": host, "port": port, "database": "postgres"},
        )
        monkeypatch.setattr(
            "vecten.mcp_onboarding.LocalProvider",
            lambda *a, **kw: SimpleNamespace(get_secret=lambda name: "fixture-master"),
        )
        native_connect = psycopg.connect
        # This throw-away Docker server has no TLS certificate; only this test disables TLS.
        monkeypatch.setattr(
            "psycopg.connect", lambda **kw: native_connect(**(kw | {"sslmode": "disable"}))
        )
        rows = catalog_rows(tmp_path, config, ["core"], "postgres")
        draft = introspection_document(rows, "postgres")
        model = tmp_path / "osi"
        model.mkdir()
        (model / "datasets.json").write_text(json.dumps(draft))
        assert validate_osi(model, warehouse_kind="postgres").ok
        assert draft["semantic_model"][0]["datasets"][0]["name"] == "orders"


def test_owner_helpers_bound_grants_and_future_objects(warehouse):
    from vecten_shared.identity_setup import postgres_access_helpers

    a = adapter(warehouse)
    a.create_group("reporting")
    with warehouse() as c:
        c.execute("CREATE ROLE model_owner NOLOGIN")
        c.execute("CREATE SCHEMA staging AUTHORIZATION model_owner")
        c.execute("CREATE TABLE staging.old_data(value int)")
        c.execute("CREATE VIEW staging.old_view AS SELECT * FROM staging.old_data")
        c.execute(postgres_access_helpers())
        c.execute(postgres_access_helpers())
    a.schema_grant("reporting", "staging")
    with warehouse() as c:
        c.execute("SET ROLE model_owner")
        c.execute("CREATE TABLE staging.future_data(value int)")
        c.execute("RESET ROLE")
        for table in ("old_data", "old_view", "future_data"):
            assert c.execute(
                "SELECT has_table_privilege('reporting', %s, 'SELECT')", (f"staging.{table}",)
            ).fetchone()[0]
        assert c.execute("SELECT has_schema_privilege('reporting', 'staging', 'USAGE')").fetchone()[
            0
        ]
    a.check_scope({"test_jane", "adopted_jan", "reporting"})
    a.schema_grant("reporting", "staging", False)
    with warehouse() as c:
        c.execute("SET ROLE model_owner")
        c.execute("CREATE TABLE staging.after_revoke(value int)")
        c.execute("RESET ROLE")
        for table in ("old_data", "old_view", "future_data", "after_revoke"):
            assert not c.execute(
                "SELECT has_table_privilege('reporting', %s, 'SELECT')", (f"staging.{table}",)
            ).fetchone()[0]
    a.dataset_grant("reporting", "staging.old_data")
    with warehouse() as c:
        assert c.execute("SELECT has_schema_privilege('reporting', 'staging', 'USAGE')").fetchone()[
            0
        ]
        assert c.execute(
            "SELECT has_table_privilege('reporting', 'staging.old_data', 'SELECT')"
        ).fetchone()[0]
        assert not c.execute(
            "SELECT has_table_privilege('reporting', 'staging.future_data', 'SELECT')"
        ).fetchone()[0]
    a.dataset_grant("reporting", "staging.old_data", False)
    for target in ("provisioner", "impersonator", "postgres", "model_owner", "public"):
        with pytest.raises((ValueError, psycopg.errors.InsufficientPrivilege)):
            a.schema_grant(target, "staging")
    with pytest.raises(ValueError, match="helper"):
        a.schema_grant("reporting", "public")
    with warehouse("provisioner", "fixture-provisioner") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM vecten_access.helpers")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM staging.old_data")
    with warehouse() as c:
        c.execute("CREATE ROLE new_model_owner NOLOGIN")
        c.execute("GRANT CREATE ON SCHEMA staging TO new_model_owner")
    with pytest.raises(ValueError, match="creators changed"):
        a.schema_grant("reporting", "staging")
    with warehouse() as c:
        assert not c.execute(
            "SELECT has_table_privilege('reporting', 'staging.old_data', 'SELECT')"
        ).fetchone()[0]
        c.execute(postgres_access_helpers())
    a.schema_grant("reporting", "staging")
    a.schema_grant("reporting", "staging", False)
    a.schema_grant("analyst", "core")
    a.schema_grant("analyst", "core", False)


def test_rds_superuser_and_its_members_are_not_creators_needing_a_helper(warehouse):
    # RDS lets rds_superuser and every role inheriting it create in any schema without an ACL
    # entry; an explicit grant stands in for that here.
    from vecten_shared.identity_setup import postgres_access_helpers

    a = adapter(warehouse)
    a.create_group("snapshot_readers")
    with warehouse() as c:
        c.execute("CREATE ROLE snapshot_owner NOLOGIN")
        c.execute("CREATE SCHEMA snapshots AUTHORIZATION snapshot_owner")
        c.execute(postgres_access_helpers())
        c.execute("CREATE ROLE rds_superuser NOLOGIN")
        c.execute("CREATE ROLE rds_master NOLOGIN IN ROLE rds_superuser")
        c.execute("GRANT CREATE ON SCHEMA snapshots TO rds_superuser")
    try:
        a.schema_grant("snapshot_readers", "snapshots")
        a.schema_grant("snapshot_readers", "snapshots", False)
    finally:
        with warehouse() as c:
            c.execute("REVOKE CREATE ON SCHEMA snapshots FROM rds_superuser")
            c.execute("DROP ROLE rds_master")
            c.execute("DROP ROLE rds_superuser")
            c.execute("DROP OWNED BY snapshot_readers")
            c.execute("DROP ROLE snapshot_readers")


def test_scope_rejects_helper_owned_by_managed_role(warehouse):
    a = adapter(warehouse)
    with warehouse() as c:
        c.execute("ALTER TABLE vecten_access.helpers OWNER TO reporting")
    try:
        with pytest.raises(ValueError, match="helper.*owner"):
            a.check_scope({"test_jane", "adopted_jan", "reporting"})
    finally:
        with warehouse() as c:
            c.execute("ALTER TABLE vecten_access.helpers OWNER TO postgres")


def test_scope_rejects_unexpected_future_privileges(warehouse):
    a = adapter(warehouse)
    with warehouse() as c:
        c.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA core "
            "GRANT INSERT ON TABLES TO provisioner"
        )
    try:
        with pytest.raises(ValueError, match="future"):
            a.check_scope({"test_jane", "adopted_jan", "reporting"})
    finally:
        with warehouse() as c:
            c.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA core "
                "REVOKE INSERT ON TABLES FROM provisioner"
            )
