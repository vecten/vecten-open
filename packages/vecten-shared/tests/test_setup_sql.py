import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from vecten_shared.sql import setup_sql_path, setup_variables

# Every package's test directory is a package named `tests`, so a relative import resolves
# against whichever of them pytest imported first; load the sibling helper by path instead.
_spec = importlib.util.spec_from_file_location(
    "vecten_shared_tests_docker_util", Path(__file__).with_name("docker_util.py")
)
docker_util = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = docker_util
_spec.loader.exec_module(docker_util)

needs_docker = pytest.mark.skipif(not docker_util.docker_available(), reason="no docker daemon")

SUPERUSER_PASSWORD = "super"
MASTER = "vecten_master"
MASTER_PASSWORD = "x"
# The entrypoint executes a top-level *.sql in the init directory itself, so the script sits in
# a subdirectory and runs only when a test runs it as the master.
SCRIPT = "/docker-entrypoint-initdb.d/vecten/warehouse_setup.sql"
PASSWORDS = {
    "AIRFLOW_DB_PASSWORD": "a1",
    "MCP_STATE_DB_PASSWORD": "m1",
    "LOADER_PASSWORD": "l1",
    "PROVISIONER_PASSWORD": "p1",
    "IMPERSONATOR_PASSWORD": "i1",
}
ALL = {
    "with_airflow": "true",
    "with_mcp_state": "true",
    "with_warehouse": "true",
    "with_mcp": "true",
}
WAREHOUSE_ROLES = {"warehouse_owner", "loader", "analyst", "provisioner", "impersonator"}


@pytest.fixture
def instance(tmp_path):
    """An instance whose master is a CREATEROLE CREATEDB login that is not a superuser.

    RDS's master owns `postgres` and `template1`, so the fixture hands them to the master the
    same way; the superuser exists only to inspect the result.
    """
    init = tmp_path / "init"
    (init / "vecten").mkdir(parents=True)
    shutil.copy(setup_sql_path(), init / "vecten" / "warehouse_setup.sql")
    with docker_util.pg_container(init, {"POSTGRES_PASSWORD": SUPERUSER_PASSWORD}) as (
        connect,
        name,
    ):
        with connect("postgres", SUPERUSER_PASSWORD, "postgres") as c:
            c.execute(
                f"create role {MASTER} login createrole createdb password '{MASTER_PASSWORD}'"
            )
            c.execute(f"alter database postgres owner to {MASTER}")
            c.execute(f"alter database template1 owner to {MASTER}")
            c.commit()
        yield connect, name


def run_setup(
    name: str, variables: dict[str, str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    cmd = ["docker", "exec", "-e", f"PGPASSWORD={MASTER_PASSWORD}"]
    for k, v in (PASSWORDS if env is None else env).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [
        name,
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        MASTER,
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    for k, v in variables.items():
        cmd += ["-v", f"{k}={v}"]
    cmd += ["-f", SCRIPT]
    return subprocess.run(cmd, capture_output=True, text=True)


def setup_ok(name: str, variables: dict[str, str], env: dict[str, str] | None = None) -> None:
    result = run_setup(name, variables, env)
    assert result.returncode == 0, result.stderr
    # A REVOKE by a role that does not own the object only warns, which would leave CONNECT
    # with PUBLIC while the script exits 0.
    assert "WARNING" not in result.stderr, result.stderr


def superuser(connect, db: str = "postgres"):
    return connect("postgres", SUPERUSER_PASSWORD, db)


def roles(connect) -> set[str]:
    with superuser(connect) as c:
        return {r[0] for r in c.execute("select rolname from pg_roles where rolname !~ '^pg_'")}


def databases(connect) -> set[str]:
    with superuser(connect) as c:
        return {r[0] for r in c.execute("select datname from pg_database where not datistemplate")}


def snapshot(connect) -> dict[str, list]:
    with superuser(connect) as c:
        cluster = {
            "roles": c.execute(
                "select rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,"
                " rolpassword from pg_authid order by rolname"
            ).fetchall(),
            "members": c.execute(
                "select r.rolname, u.rolname, g.rolname, m.admin_option, m.inherit_option,"
                " m.set_option from pg_auth_members m join pg_roles r on r.oid = m.roleid"
                " join pg_roles u on u.oid = m.member join pg_roles g on g.oid = m.grantor"
                " order by 1, 2, 3"
            ).fetchall(),
            "databases": c.execute(
                "select datname, pg_get_userbyid(datdba), datacl::text from pg_database"
                " order by datname"
            ).fetchall(),
            "settings": c.execute(
                "select setdatabase, setrole, setconfig::text from pg_db_role_setting order by 1, 2"
            ).fetchall(),
        }
    with superuser(connect, "warehouse") as c:
        cluster["schemas"] = c.execute(
            "select nspname, pg_get_userbyid(nspowner), nspacl::text from pg_namespace"
            " order by nspname"
        ).fetchall()
        cluster["default_acl"] = c.execute(
            "select pg_get_userbyid(defaclrole), defaclnamespace::regnamespace::text,"
            " defaclobjtype, defaclacl::text from pg_default_acl order by 1, 2, 3"
        ).fetchall()
    return cluster


def test_setup_variables_for_minimal_instance():
    assert setup_variables(["airflow", "mcp_state", "warehouse"], True) == ALL


def test_setup_variables_for_state_instance_keep_warehouse_roles_off():
    assert setup_variables(["airflow", "mcp_state"], True) == {
        "with_airflow": "true",
        "with_mcp_state": "true",
        "with_warehouse": "false",
        "with_mcp": "false",
    }


def test_setup_variables_for_warehouse_instance():
    assert setup_variables(["warehouse"], True) == {
        "with_airflow": "false",
        "with_mcp_state": "false",
        "with_warehouse": "true",
        "with_mcp": "true",
    }


def test_setup_variables_without_mcp():
    assert setup_variables(["airflow", "warehouse"], False) == {
        "with_airflow": "true",
        "with_mcp_state": "false",
        "with_warehouse": "true",
        "with_mcp": "false",
    }


def test_setup_variables_refuse_an_unknown_database():
    with pytest.raises(ValueError, match="dagster"):
        setup_variables(["dagster"], False)


@pytest.mark.docker
@needs_docker
def test_setup_as_non_superuser_master_is_idempotent(instance):
    connect, name = instance
    with superuser(connect) as c:
        assert c.execute(
            "select rolsuper, rolcreaterole, rolcreatedb from pg_roles where rolname = %s",
            (MASTER,),
        ).fetchone() == (False, True, True)
    setup_ok(name, ALL, PASSWORDS | {"MASTER_ROLE": MASTER})
    first = snapshot(connect)
    setup_ok(name, ALL, {k: "changed" for k in PASSWORDS} | {"MASTER_ROLE": MASTER})
    assert snapshot(connect) == first
    with connect("loader", "l1", "warehouse"):
        pass
    assert owner_login(connect) == (False, None)


@pytest.mark.docker
@needs_docker
def test_rds_superuser_and_its_members_get_no_access_helper(instance):
    # RDS lets rds_superuser and every role inheriting it, the master among them, create in any
    # schema without an ACL entry; an explicit grant stands in for that here.
    connect, name = instance
    setup_ok(name, ALL, PASSWORDS | {"MASTER_ROLE": MASTER})
    with superuser(connect, "warehouse") as c:
        c.execute("create role rds_superuser nologin")
        c.execute(f"grant rds_superuser to {MASTER} with inherit true, set true")
        c.execute("grant create on schema core, marts, staging, snapshots to rds_superuser")
        c.commit()
    setup_ok(name, ALL, PASSWORDS | {"MASTER_ROLE": MASTER})
    with superuser(connect, "warehouse") as c:
        owners = {
            r[0]
            for r in c.execute(
                "select distinct owner_oid::regrole::text from vecten_access.helpers"
            )
        }
    assert owners == {"loader"}


@pytest.mark.docker
@needs_docker
def test_existing_runtime_owned_access_events_migrate_without_losing_rows(instance):
    import time

    import psycopg

    connect, name = instance
    with connect(MASTER, MASTER_PASSWORD, "postgres") as conn:
        conn.autocommit = True
        conn.execute("CREATE ROLE mcp_state LOGIN PASSWORD 'm1'")
        conn.execute(f"GRANT mcp_state TO {MASTER} WITH SET TRUE, INHERIT FALSE")
        conn.execute("CREATE DATABASE mcp_state OWNER mcp_state")
    with connect("mcp_state", "m1", "mcp_state") as conn:
        conn.execute(
            "CREATE TABLE access_events (id text PRIMARY KEY, at double precision NOT NULL, "
            "principal_id text, operation text NOT NULL, subject text, outcome text NOT NULL)"
        )
        conn.execute(
            "INSERT INTO access_events VALUES ('original',%s,'person','login','person','allowed')",
            (time.time(),),
        )
        conn.commit()
    setup_ok(name, ALL)
    setup_ok(name, ALL)
    with connect("mcp_state", "m1", "mcp_state") as conn:
        assert conn.execute("SELECT id FROM access_events").fetchall() == [("original",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM access_events")


def owner_login(connect) -> tuple:
    with superuser(connect) as c:
        return c.execute(
            "select rolcanlogin, rolpassword from pg_authid where rolname = 'warehouse_owner'"
        ).fetchone()


@pytest.mark.docker
@needs_docker
def test_rerun_turns_a_login_warehouse_owner_into_nologin(instance):
    connect, name = instance
    # The master created the role with a login, as the script once did.
    with connect(MASTER, MASTER_PASSWORD, "postgres") as c:
        c.execute("create role warehouse_owner login password 'w1'")
        c.commit()
    assert owner_login(connect)[0] is True
    setup_ok(name, ALL, PASSWORDS | {"MASTER_ROLE": MASTER})
    assert owner_login(connect) == (False, None)
    first = snapshot(connect)
    setup_ok(name, ALL, PASSWORDS | {"MASTER_ROLE": MASTER})
    assert snapshot(connect) == first


@pytest.mark.docker
@needs_docker
def test_state_instance_holds_no_person_roles(instance):
    connect, name = instance
    setup_ok(name, ALL | {"with_warehouse": "false"})
    assert databases(connect) == {"postgres", "airflow", "mcp_state"}
    assert not WAREHOUSE_ROLES & roles(connect)
    assert {"airflow", "mcp_state"} <= roles(connect)


@pytest.mark.docker
@needs_docker
def test_warehouse_instance_only(instance):
    connect, name = instance
    setup_ok(
        name,
        {
            "with_airflow": "false",
            "with_mcp_state": "false",
            "with_warehouse": "true",
            "with_mcp": "true",
        },
    )
    assert databases(connect) == {"postgres", "warehouse"}
    assert WAREHOUSE_ROLES <= roles(connect)
    assert not {"airflow", "mcp_state"} & roles(connect)
    with connect("loader", "l1", "warehouse") as c:
        c.execute("create table core.t (x int)")
        c.execute("insert into core.t values (1)")
    with connect("provisioner", "p1", "warehouse") as c:
        c.execute("create role person login password 'pp' in role analyst")
    with connect("person", "pp", "warehouse") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select x from core.t")
    # The MCP's reconciliation applies the default group's `core` grant through the helper.
    with connect("provisioner", "p1", "warehouse") as c:
        [(function,)] = c.execute(
            "select function_name from vecten_access.helpers where schema_name = 'core'"
        ).fetchall()
        c.execute(f"select vecten_access.{function}('analyst', true, null)")
        c.commit()
    with connect("person", "pp", "warehouse") as c:
        assert c.execute("select x from core.t").fetchone() == (1,)


@pytest.mark.docker
@needs_docker
def test_public_has_no_connect(instance):
    connect, name = instance
    setup_ok(name, ALL)
    with superuser(connect) as c:
        connectable = c.execute(
            "select datname, has_database_privilege('public', datname, 'CONNECT')"
            " from pg_database where datallowconn"
        ).fetchall()
    assert {d for d, _ in connectable} == {
        "postgres",
        "template1",
        "airflow",
        "mcp_state",
        "warehouse",
    }
    assert [d for d, public in connectable if public] == []


@pytest.mark.docker
@needs_docker
def test_impersonator_has_login_and_no_membership(instance):
    connect, name = instance
    setup_ok(name, ALL)
    with superuser(connect) as c:
        assert c.execute(
            "select rolcanlogin, rolcreaterole, rolcreatedb, rolsuper from pg_roles"
            " where rolname = 'impersonator'"
        ).fetchone() == (True, False, False, False)
        assert not c.execute(
            "select 1 from pg_auth_members m join pg_roles u on u.oid = m.member"
            " where u.rolname = 'impersonator'"
        ).fetchall()


@pytest.mark.docker
@needs_docker
def test_platform_only_creates_no_provisioner(instance):
    connect, name = instance
    setup_ok(name, setup_variables(["airflow", "warehouse"], False))
    assert databases(connect) == {"postgres", "airflow", "warehouse"}
    assert not {"provisioner", "impersonator", "mcp_state"} & roles(connect)
    assert {"airflow", "warehouse_owner", "loader", "analyst"} <= roles(connect)


@pytest.mark.docker
@needs_docker
@pytest.mark.parametrize("value", ["TRUE", "yes", ""])
def test_variables_must_be_exactly_true_or_false(instance, value):
    connect, name = instance
    result = run_setup(name, ALL | {"with_warehouse": value})
    assert result.returncode != 0
    assert "with_warehouse must be exactly true or false" in result.stderr
    assert databases(connect) == {"postgres"}


@pytest.mark.docker
@needs_docker
def test_missing_variable_is_refused(instance):
    connect, name = instance
    result = run_setup(name, {k: v for k, v in ALL.items() if k != "with_airflow"})
    assert result.returncode != 0
    assert "with_airflow must be exactly true or false" in result.stderr
    assert databases(connect) == {"postgres"}


def area_schemas(connect):
    with superuser(connect, "warehouse") as c:
        owners = dict(
            c.execute(
                "select nspname, pg_get_userbyid(nspowner) from pg_namespace"
                " where nspname ~ '^(core|marts)_'"
            ).fetchall()
        )
        helpers = {r[0] for r in c.execute("select schema_name from vecten_access.helpers")}
    return owners, helpers


@pytest.mark.docker
@needs_docker
def test_declared_areas_get_their_schemas_as_loader_and_a_helper_each(instance):
    connect, name = instance
    setup_ok(name, ALL | {"areas": "finance,payroll"})
    owners, helpers = area_schemas(connect)
    expected = {"core_finance", "marts_finance", "core_payroll", "marts_payroll"}
    assert owners == dict.fromkeys(expected, "loader")
    assert expected <= helpers
    # A rerun without the list keeps the schemas and their helpers.
    setup_ok(name, ALL)
    assert area_schemas(connect) == (owners, helpers)


@pytest.mark.docker
@needs_docker
def test_a_schema_dbt_created_in_an_area_gets_a_helper_on_the_next_setup(instance):
    connect, name = instance
    setup_ok(name, ALL)
    with connect("loader", "l1", "warehouse") as c:
        c.execute("create schema marts_finance")
        c.execute("create schema marts_fin_ops")
    setup_ok(name, ALL)
    _, helpers = area_schemas(connect)
    assert "marts_finance" in helpers and "marts_fin_ops" not in helpers


@pytest.mark.docker
@needs_docker
@pytest.mark.parametrize("value", ["Finance", "fin_ops", "finance;drop", "finance,", ","])
def test_an_area_list_outside_the_name_rule_is_refused(instance, value):
    connect, name = instance
    result = run_setup(name, ALL | {"areas": value})
    assert result.returncode != 0
    assert "areas must be" in result.stderr
    assert databases(connect) == {"postgres"}
