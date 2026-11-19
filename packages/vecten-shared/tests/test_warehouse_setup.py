import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from vecten_shared.sql import local_init_script_path, setup_sql_path

# Every package's test directory is a package named `tests`, so a relative import resolves
# against whichever of them pytest imported first; load the sibling helper by path instead.
_spec = importlib.util.spec_from_file_location(
    "vecten_shared_tests_docker_util", Path(__file__).with_name("docker_util.py")
)
docker_util = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = docker_util
_spec.loader.exec_module(docker_util)
pg_container = docker_util.pg_container

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_util.docker_available(), reason="no docker daemon"),
]
MCP_VARS = ("MCP_STATE_DB_PASSWORD", "PROVISIONER_PASSWORD", "IMPERSONATOR_PASSWORD")
ENV = {
    "POSTGRES_PASSWORD": "master",
    "AIRFLOW_DB_PASSWORD": "a1",
    "MCP_STATE_DB_PASSWORD": "m1",
    "LOADER_PASSWORD": "l1",
    "PROVISIONER_PASSWORD": "p1",
    "IMPERSONATOR_PASSWORD": "i1",
    "VECTEN_WITH_MCP": "true",
}
ENV_WITHOUT_MCP = {k: v for k, v in ENV.items() if k not in MCP_VARS} | {"VECTEN_WITH_MCP": "false"}
INIT_SCRIPT = "/docker-entrypoint-initdb.d/10-vecten.sh"


@pytest.fixture
def init_dir(tmp_path):
    d = tmp_path / "init"
    (d / "vecten").mkdir(parents=True)
    shutil.copy(setup_sql_path(), d / "vecten" / "warehouse_setup.sql")
    shutil.copy(local_init_script_path(), d / "10-vecten.sh")
    return d


def rerun_init(name: str, *env_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", name, "env", *env_args, "bash", INIT_SCRIPT],
        capture_output=True,
        text=True,
    )


def role_names(connect) -> set[str]:
    with connect("postgres", "master", "postgres") as c:
        return {r[0] for r in c.execute("select rolname from pg_roles")}


def test_roles_databases_and_revokes(init_dir):
    with pg_container(init_dir, ENV) as (connect, _):
        with connect("postgres", "master", "postgres") as c:
            dbs = {
                r[0] for r in c.execute("select datname from pg_database where not datistemplate")
            }
            assert {"airflow", "mcp_state", "warehouse"} <= dbs
            roles = {
                r[0]: (r[1], r[2], r[3])
                for r in c.execute(
                    "select rolname, rolcanlogin, rolcreaterole, rolinherit from pg_roles"
                )
            }
            assert roles["loader"][0] and roles["provisioner"] == (True, True, True)
            assert roles["impersonator"] == (True, False, False) and roles["analyst"][0] is False
            assert roles["warehouse_owner"][0] is False
            owners = dict(
                c.execute(
                    "select d.datname, r.rolname from pg_database d"
                    " join pg_roles r on r.oid = d.datdba"
                    " where d.datname in ('airflow','mcp_state','warehouse')"
                )
            )
            assert owners == {
                "airflow": "airflow",
                "mcp_state": "mcp_state_maintenance",
                "warehouse": "warehouse_owner",
            }
        with connect("postgres", "master", "warehouse") as c:
            schemas = {
                r[0] for r in c.execute("select schema_name from information_schema.schemata")
            }
            assert {"staging", "snapshots", "core", "marts"} <= schemas


def test_audit_is_append_only_even_for_runtime_sql_and_retention_is_bounded(init_dir):
    import time

    with pg_container(init_dir, ENV) as (connect, name):
        with connect("mcp_state", "m1", "mcp_state") as conn:
            conn.execute(
                "INSERT INTO audit_log (id, at, body) VALUES (%s,%s,%s)",
                ("current", time.time(), "{}"),
            )
            conn.execute(
                "INSERT INTO audit_log (id, at, body) VALUES (%s,%s,%s)",
                ("expired", time.time() - 100 * 86400, "{}"),
            )
            conn.execute(
                "INSERT INTO access_events VALUES (%s,%s,%s,%s,%s,%s)",
                ("event", time.time(), "actor", "login", "actor", "allowed"),
            )
            conn.commit()
            for sql in (
                "UPDATE audit_log SET body='{}'",
                "DELETE FROM audit_log",
                "DELETE FROM access_events",
                "TRUNCATE audit_log",
                "DROP TABLE audit_log",
                "SET ROLE mcp_state_maintenance",
                "ALTER DATABASE mcp_state OWNER TO mcp_state",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql)
                conn.rollback()
            conn.execute("SELECT vecten_audit.maintain(90)")
            conn.commit()
            assert conn.execute("SELECT id FROM audit_log").fetchall() == [("current",)]
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                conn.execute("SELECT vecten_audit.maintain(0)")
            conn.rollback()
        rerun = rerun_init(name)
        assert rerun.returncode == 0, rerun.stderr
        with connect("mcp_state", "m1", "mcp_state") as conn:
            assert conn.execute("SELECT id FROM audit_log").fetchall() == [("current",)]


def test_connect_revoked_from_public_and_roles_scoped(init_dir):
    with pg_container(init_dir, ENV) as (connect, _):
        with connect("postgres", "master", "postgres") as c:
            c.execute("create role probe login password 'x'")
            c.commit()
        for db in ("postgres", "template1", "airflow", "mcp_state", "warehouse"):
            with pytest.raises(psycopg.OperationalError):
                connect("probe", "x", db)
        with connect("loader", "l1", "warehouse"):
            pass
        for db in ("postgres", "airflow", "mcp_state"):
            with pytest.raises(psycopg.OperationalError):
                connect("impersonator", "i1", db)
        with connect("impersonator", "i1", "warehouse"):
            pass


def test_loader_creates_raw_schemas(init_dir):
    with pg_container(init_dir, ENV) as (connect, _):
        with connect("loader", "l1", "warehouse") as c:
            c.execute("create schema raw_probe")
            c.execute("create table raw_probe.t (x int)")
            c.execute("insert into raw_probe.t values (1)")
            assert c.execute("select x from raw_probe.t").fetchone() == (1,)


def test_identity_contract_grants(init_dir):
    with pg_container(init_dir, ENV) as (connect, _):
        with connect("loader", "l1", "warehouse") as c:
            c.execute("create table core.t (x int)")
            c.execute("insert into core.t values (1)")
            c.execute("create view marts.v as select x from core.t")
            c.execute("create table staging.s (x int)")
        with connect("postgres", "master", "warehouse") as c:
            for rel in ("core.t", "marts.v"):
                assert c.execute(
                    "select has_table_privilege('provisioner', %s, 'SELECT WITH GRANT OPTION')",
                    (rel,),
                ).fetchone()[0]
            # The MCP's reconciliation grants analyst its reads, from the state database.
            for schema in ("core", "marts"):
                assert not c.execute(
                    "select has_schema_privilege('analyst', %s, 'USAGE')", (schema,)
                ).fetchone()[0]
            assert not c.execute(
                "select 1 from pg_default_acl d cross join lateral aclexplode(d.defaclacl) a"
                " where a.grantee = 'analyst'::regrole"
            ).fetchall()
            memberships = c.execute(
                "select g.rolname, m.admin_option from pg_auth_members m"
                " join pg_roles g on g.oid = m.roleid join pg_roles u on u.oid = m.member"
                " where u.rolname = %s",
                ("provisioner",),
            ).fetchall()
            assert ("analyst", True) in memberships
            assert not c.execute(
                "select 1 from pg_auth_members m join pg_roles u on u.oid = m.member"
                " where u.rolname = 'impersonator'"
            ).fetchall()
            c.execute("create role probe login password 'x' in role analyst")
            c.commit()
        with connect("probe", "x", "warehouse") as c:
            for relation in ("core.t", "marts.v", "staging.s"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f"select x from {relation}")
                c.rollback()


def test_without_mcp_analyst_reads_core_and_marts(init_dir):
    with pg_container(init_dir, ENV_WITHOUT_MCP) as (connect, _):
        with connect("loader", "l1", "warehouse") as c:
            c.execute("create table core.t (x int)")
            c.execute("insert into core.t values (1)")
            c.execute("create table marts.m as select x from core.t")
            c.execute("create table staging.s (x int)")
        with connect("postgres", "master", "warehouse") as c:
            c.execute("create role probe login password 'x' in role analyst")
            c.commit()
        with connect("probe", "x", "warehouse") as c:
            assert c.execute("select x from core.t").fetchone() == (1,)
            assert c.execute("select x from marts.m").fetchone() == (1,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select x from staging.s")


def test_without_mcp_creates_no_mcp_roles_or_database(init_dir):
    with pg_container(init_dir, ENV_WITHOUT_MCP) as (connect, _):
        roles = role_names(connect)
        assert not {"provisioner", "impersonator", "mcp_state"} & roles
        assert {"loader", "analyst", "airflow"} <= roles
        with connect("postgres", "master", "postgres") as c:
            dbs = {r[0] for r in c.execute("select datname from pg_database")}
        assert "mcp_state" not in dbs and {"airflow", "warehouse"} <= dbs


def test_setup_sql_is_idempotent(init_dir):
    with pg_container(init_dir, ENV) as (connect, name):
        before = len(role_names(connect))
        changed = [
            f"{var}=changed" for var in ("AIRFLOW_DB_PASSWORD", "LOADER_PASSWORD", *MCP_VARS)
        ]
        rerun = rerun_init(name, *changed)
        assert rerun.returncode == 0, rerun.stderr
        assert len(role_names(connect)) == before
        with connect("loader", "l1", "warehouse"):
            pass
        with pytest.raises(psycopg.OperationalError):
            connect("loader", "changed", "warehouse")


def test_mcp_passwords_required_when_mcp_on(init_dir):
    with pg_container(init_dir, ENV_WITHOUT_MCP) as (connect, name):
        unset = [arg for var in MCP_VARS for arg in ("-u", var)]
        rerun = rerun_init(name, *unset, "VECTEN_WITH_MCP=true")
        assert rerun.returncode != 0
        assert "required when VECTEN_WITH_MCP=true" in rerun.stderr
        assert not {"provisioner", "impersonator", "mcp_state"} & role_names(connect)


def test_with_mcp_must_be_exactly_true_or_false(init_dir):
    with pg_container(init_dir, ENV_WITHOUT_MCP) as (connect, name):
        rerun = rerun_init(
            name, *(f"{k}={v}" for k, v in ENV.items() if k in MCP_VARS), "VECTEN_WITH_MCP=TRUE"
        )
        assert rerun.returncode != 0
        assert "VECTEN_WITH_MCP must be exactly true or false" in rerun.stderr
        assert not {"provisioner", "impersonator", "mcp_state"} & role_names(connect)
