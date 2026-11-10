import base64
import hashlib
import hmac
import json
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from vecten.deploy import admin
from vecten.deploy.aws import AwsProvider
from vecten.deploy.aws import tunnel as tunnel_mod
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import secret_id

from . import docker_util
from .deploy_fixtures import (
    COMMIT,
    REGION,
    STANDARD_OUTPUTS,
    STATE_BUCKET,
    STATE_HOST,
    WAREHOUSE_HOST,
    aws_credentials,
    minimal_outputs,
    render_aws,
    set_facts,
)
from .scaffold_matrix import DEPLOY, LOCAL
from .scaleway_fakes import CERTIFICATE_PEM as SCW_CERTIFICATE_PEM
from .scaleway_fakes import MASTER_PASSWORD as SCW_MASTER_PASSWORD
from .scaleway_fakes import MASTER_USER as SCW_MASTER_USER
from .scaleway_fakes import install, prepare_environment, scaleway_outputs

MASTER_USER = "vecten_master"
MASTER_PASSWORD = "master-password-value-0001"


class FakeTunnel:
    opened: list = []

    @contextmanager
    def open(self, instance_id, remote_host, remote_port, local_port, region):
        type(self).opened.append(
            {
                "instance_id": instance_id,
                "remote_host": remote_host,
                "remote_port": remote_port,
                "local_port": local_port,
                "region": region,
            }
        )
        yield


class FakeConnection:
    """Records every statement; `pg_roles` lookups answer from `roles`."""

    def __init__(self, roles: set[str]):
        self.roles = roles
        self.statements: list[str] = []
        self.params: list = []
        self.committed = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, query, params=None):
        self.statements.append(str(query))
        self.params.append(params)
        found = "pg_roles" in str(query) and params and params[0] in self.roles
        return _Cursor((1,) if found else None)

    def commit(self):
        self.committed = True


class _Cursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


@pytest.fixture
def fake_db(monkeypatch):
    state = {"roles": set(), "connections": [], "kwargs": [], "certificates": []}

    def connect(kwargs):
        state["kwargs"].append(kwargs)
        if "sslrootcert" in kwargs:
            state["certificates"].append(Path(kwargs["sslrootcert"]).read_bytes())
        conn = FakeConnection(state["roles"])
        state["connections"].append(conn)
        return conn

    monkeypatch.setattr(admin, "default_connect", connect)
    return state


def _prepare(root: Path, monkeypatch) -> None:
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("VECTEN_TUNNEL_RUNNER", f"{__name__}:FakeTunnel")
    monkeypatch.setattr(FakeTunnel, "opened", [])
    set_facts(root, bootstrapped=True, state_bucket=STATE_BUCKET, deployed_commit=COMMIT)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "proj")
    _prepare(root, monkeypatch)
    return root


@contextmanager
def cloud(root: Path, monkeypatch, outputs: dict = STANDARD_OUTPUTS):
    """Moto with the state bucket, the stored outputs and the instances' master secrets."""
    aws_credentials(monkeypatch, root)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=STATE_BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        sm = boto3.client("secretsmanager", region_name=REGION)
        stored = json.loads(json.dumps(outputs))
        for key, instance in stored["postgres"].items():
            created = sm.create_secret(
                Name=f"rds!db-{key}",
                SecretString=json.dumps({"username": MASTER_USER, "password": MASTER_PASSWORD}),
            )
            instance["master_secret"] = created["ARN"]
        s3.put_object(Bucket=STATE_BUCKET, Key="prod/outputs.json", Body=json.dumps(stored))
        yield sm


def _stored(sm, name: str) -> str:
    return sm.get_secret_value(SecretId=secret_id("acme", "prod", name))["SecretString"]


def doc(result):
    return json.loads(result.stdout)


def _scram_matches(verifier: str, password: str) -> bool:
    method, rest = verifier.split("$", 1)
    params, keys = rest.split("$")
    iterations, salt = params.split(":")
    stored_key, server_key = keys.split(":")
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(salt), int(iterations)
    )
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    expected_server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return (
        method == "SCRAM-SHA-256"
        and base64.b64decode(stored_key) == hashlib.sha256(client_key).digest()
        and base64.b64decode(server_key) == expected_server
    )


# Creating the account


def test_creates_admin_through_the_tunnel_and_stores_only_the_secret(
    invoke, project, fake_db, monkeypatch
):
    with cloud(project, monkeypatch) as sm:
        r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
        assert r.exit_code == 0, r.stdout
        password = _stored(sm, "ADMIN_JANE_PASSWORD")
    [opened] = FakeTunnel.opened
    assert opened["instance_id"] == "i-0mcp"
    assert opened["remote_host"] == WAREHOUSE_HOST
    assert opened["remote_port"] == 5432
    assert opened["region"] == REGION
    [kwargs] = fake_db["kwargs"]
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == opened["local_port"]
    assert kwargs["user"] == MASTER_USER and kwargs["password"] == MASTER_PASSWORD
    assert kwargs["dbname"] == "postgres"
    assert kwargs["sslmode"] == "require"

    [conn] = fake_db["connections"]
    sql = conn.statements
    create = [s for s in sql if s.startswith("CREATE ROLE admin_jane")]
    assert len(create) == 1
    assert "LOGIN CREATEROLE CREATEDB PASSWORD 'SCRAM-SHA-256$" in create[0]
    verifier = create[0].split("PASSWORD '", 1)[1].rstrip("'")
    assert _scram_matches(verifier, password)
    assert password not in "\n".join(sql)
    assert "GRANT rds_superuser TO admin_jane" in sql
    grant_connect = sql.index("GRANT CONNECT ON DATABASE warehouse TO admin_jane")
    assert sql[grant_connect - 1] == "SET ROLE warehouse_owner"
    assert sql[grant_connect + 1] == "RESET ROLE"
    assert conn.committed and conn.closed

    data = doc(r)
    assert data["role"] == "admin_jane"
    assert data["created"] is True
    assert data["secret_id"] == "vecten/acme/prod/admin/ADMIN_JANE_PASSWORD"
    assert password not in r.stdout and MASTER_PASSWORD not in r.stdout


def test_human_output_prints_the_secret_id_only(invoke, project, fake_db, monkeypatch):
    with cloud(project, monkeypatch) as sm:
        r = invoke("user", "add-admin", "jane", "--env", "prod")
        password = _stored(sm, "ADMIN_JANE_PASSWORD")
    assert r.exit_code == 0, r.stderr
    assert "vecten/acme/prod/admin/ADMIN_JANE_PASSWORD" in r.stdout
    assert password not in r.stdout + r.stderr


def test_an_existing_admin_with_a_stored_password_is_left_alone_and_regranted(
    invoke, project, fake_db, monkeypatch
):
    fake_db["roles"].add("admin_jane")
    with cloud(project, monkeypatch) as sm:
        sm.create_secret(
            Name=secret_id("acme", "prod", "ADMIN_JANE_PASSWORD"), SecretString="kept-value"
        )
        r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
        assert _stored(sm, "ADMIN_JANE_PASSWORD") == "kept-value"
    assert r.exit_code == 0, r.stdout
    [conn] = fake_db["connections"]
    assert not any(s.startswith(("CREATE ROLE", "ALTER ROLE")) for s in conn.statements)
    assert "GRANT rds_superuser TO admin_jane" in conn.statements
    assert "GRANT CONNECT ON DATABASE warehouse TO admin_jane" in conn.statements
    data = doc(r)
    assert data["created"] is False and data["password_rotated"] is False


def test_an_existing_admin_without_a_stored_password_gets_a_new_one(
    invoke, project, fake_db, monkeypatch
):
    fake_db["roles"].add("admin_jane")
    with cloud(project, monkeypatch) as sm:
        r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
        password = _stored(sm, "ADMIN_JANE_PASSWORD")
    assert r.exit_code == 0, r.stdout
    [conn] = fake_db["connections"]
    assert not any(s.startswith("CREATE ROLE") for s in conn.statements)
    [alter] = [s for s in conn.statements if s.startswith("ALTER ROLE admin_jane PASSWORD")]
    assert _scram_matches(alter.split("PASSWORD '", 1)[1].rstrip("'"), password)
    assert conn.statements.index(alter) < conn.statements.index("GRANT rds_superuser TO admin_jane")
    data = doc(r)
    assert data["created"] is False and data["password_rotated"] is True
    assert password not in r.stdout


def test_a_failed_connection_is_labelled_as_connecting(invoke, project, monkeypatch):
    class Unreachable(Exception):
        pass

    def connect(kwargs):
        raise Unreachable(f"no route; password {kwargs['password']}")

    monkeypatch.setattr(admin, "default_connect", connect)
    with cloud(project, monkeypatch):
        r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 5
    error = doc(r)["error"]
    assert error == "connecting to the warehouse failed: Unreachable"


def test_a_failed_commit_on_close_reports_the_type_only(invoke, project, fake_db, monkeypatch):
    class CommitRefused(Exception):
        pass

    def exit_(self, *exc):
        raise CommitRefused(f"server closed; password {MASTER_PASSWORD}")

    monkeypatch.setattr(FakeConnection, "__exit__", exit_)
    with cloud(project, monkeypatch):
        r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 5
    error = doc(r)["error"]
    assert error == "closing the warehouse connection failed: CommitRefused"


def test_uppercase_first_name_lowercases(invoke, project, fake_db, monkeypatch):
    with cloud(project, monkeypatch) as sm:
        r = invoke("user", "add-admin", "Jane", "--env", "prod", "--json")
        assert _stored(sm, "ADMIN_JANE_PASSWORD")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["role"] == "admin_jane"


def test_minimal_environment_tunnels_through_main_to_the_state_instance(
    invoke, project, fake_db, monkeypatch
):
    with cloud(project, monkeypatch, outputs=minimal_outputs()):
        r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    [opened] = FakeTunnel.opened
    assert opened["instance_id"] == "i-0main"
    assert opened["remote_host"] == STATE_HOST


# Refusals


@pytest.mark.parametrize("name", ["ja-ne", "1jane", "j" * 42, "ja ne"])
def test_invalid_first_name_is_usage(invoke, project, fake_db, name):
    r = invoke("user", "add-admin", name, "--env", "prod", "--json")
    assert r.exit_code == 2
    assert FakeTunnel.opened == [] and fake_db["connections"] == []


def test_needs_a_first_name_and_an_environment(invoke, project):
    assert invoke("user", "add-admin", "--env", "prod", "--json").exit_code == 2
    r = invoke("user", "add-admin", "jane", "--json")
    assert r.exit_code == 2
    assert "--env" in doc(r)["error"]


def test_mcp_only_over_an_existing_warehouse_is_refused(invoke, tmp_path, fake_db, monkeypatch):
    root = tmp_path / "existing"
    answers = {
        **LOCAL,
        "has_warehouse": "yes",
        "warehouse_url": "postgres://dw.internal:5432/analytics",
        **DEPLOY,
        "cloud": "aws",
    }
    render_project(root, ScaffoldParams.from_answers(answers, {}))
    _prepare(root, monkeypatch)
    r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "already has an account" in doc(r)["error"]
    assert FakeTunnel.opened == []


def test_snowflake_is_refused(invoke, tmp_path, fake_db, monkeypatch):
    root = tmp_path / "snow"
    answers = {**LOCAL, "sources": ["hubspot"], "warehouse": "snowflake", **DEPLOY, "cloud": "aws"}
    render_project(root, ScaffoldParams.from_answers(answers, {}))
    _prepare(root, monkeypatch)
    r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "Snowflake" in doc(r)["error"]
    assert FakeTunnel.opened == []


def test_not_deployed_names_deploy(invoke, project, fake_db):
    set_facts(project, deployed_commit=None)
    r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 4
    assert "vecten deploy --env prod" in doc(r)["error"]


def test_a_failing_statement_reports_no_secret(invoke, project, fake_db, monkeypatch):
    class Refused(Exception):
        pass

    original = FakeConnection.execute

    def execute(self, query, params=None):
        if str(query).startswith("GRANT rds_superuser"):
            raise Refused(f"permission denied; password was {MASTER_PASSWORD}")
        return original(self, query, params)

    monkeypatch.setattr(FakeConnection, "execute", execute)
    with cloud(project, monkeypatch):
        r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 5
    error = doc(r)["error"]
    assert "Refused" in error and MASTER_PASSWORD not in error


# The default connection and tunnel


def test_missing_psycopg_is_a_prerequisite(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(Failure) as caught:
        admin.connect_with_psycopg({"host": "127.0.0.1"})
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "psycopg" in caught.value.message


def test_missing_session_manager_plugin_names_its_install_page(monkeypatch):
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "session-manager-plugin" else f"/bin/{name}"
    )
    with pytest.raises(Failure) as caught:
        with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
            pass
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "session-manager-working-with-install-plugin" in caught.value.message


def test_missing_aws_cli_is_a_prerequisite(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "aws" else f"/bin/{name}")
    with pytest.raises(Failure) as caught:
        with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
            pass
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "AWS CLI" in caught.value.message


class FakeProcess:
    """A session process whose group signals are recorded in `signals`."""

    pid = 4242

    def __init__(self, code=None, ignores_term=False):
        self.code = code
        self.ignores_term = ignores_term
        self.waits: list = []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if timeout is not None and self.ignores_term:
            raise subprocess.TimeoutExpired("aws", timeout)
        return 0


@pytest.fixture
def session(monkeypatch):
    seen = {"signals": [], "process": FakeProcess(), "stderr": b"", "port_open": True}

    def popen(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        kwargs["stderr"].write(seen["stderr"])
        kwargs["stderr"].flush()
        return seen["process"]

    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", popen)
    monkeypatch.setattr(tunnel_mod, "port_open", lambda port: seen["port_open"])
    monkeypatch.setattr(
        tunnel_mod.os, "killpg", lambda pgid, sig: seen["signals"].append((pgid, sig))
    )
    return seen


GROUP_STOP = [(FakeProcess.pid, signal.SIGTERM), (FakeProcess.pid, signal.SIGKILL)]


def test_session_command_line_and_group_stop(session):
    with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
        assert session["signals"] == []
    assert session["args"] == [
        "aws",
        "ssm",
        "start-session",
        "--target",
        "i-0mcp",
        "--document-name",
        "AWS-StartPortForwardingSessionToRemoteHost",
        "--parameters",
        "host=db.example,portNumber=5432,localPortNumber=15432",
        "--region",
        REGION,
    ]
    # Its own session, so the plugin `aws` starts shares a group the stop can signal.
    assert session["kwargs"]["start_new_session"] is True
    assert session["signals"] == GROUP_STOP
    assert session["process"].waits[0] == tunnel_mod.STOP_TIMEOUT


def test_an_error_in_the_body_still_stops_the_group(session):
    with pytest.raises(RuntimeError):
        with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
            raise RuntimeError("statement failed")
    assert session["signals"] == GROUP_STOP


def test_a_group_that_ignores_sigterm_is_killed(session):
    session["process"] = FakeProcess(ignores_term=True)
    with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
        pass
    assert session["signals"] == GROUP_STOP
    assert session["process"].waits == [tunnel_mod.STOP_TIMEOUT, None]


def test_a_session_that_exits_early_is_a_tool_failure_and_stops_the_group(session):
    session["process"] = FakeProcess(code=255)
    session["port_open"] = False
    session["stderr"] = b"An error occurred (TargetNotConnected) when calling StartSession\n"
    with pytest.raises(Failure) as caught:
        with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
            pass
    assert caught.value.code == ExitCode.TOOL_FAILED
    assert "TargetNotConnected" in caught.value.message
    # The leader exited, yet the plugin it started may live: the group is still signalled,
    # and the leader is reaped without a timed wait.
    assert session["signals"] == GROUP_STOP
    assert session["process"].waits == [None]


def test_an_unreaped_leader_answering_eperm_counts_as_gone(session, monkeypatch):
    attempts = []

    def eperm(pgid, sig):
        attempts.append(sig)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(tunnel_mod.os, "killpg", eperm)
    session["process"] = FakeProcess(code=0)
    with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
        pass
    assert attempts == [signal.SIGTERM, signal.SIGKILL]
    assert session["process"].waits == [None]


def test_sighup_is_watched_while_the_tunnel_is_open_and_restored_after(session):
    before = signal.getsignal(signal.SIGHUP)
    with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
        assert signal.getsignal(signal.SIGHUP) is tunnel_mod._hangup
    assert signal.getsignal(signal.SIGHUP) is before


def test_a_hangup_inside_the_tunnel_stops_the_group_and_restores_the_handler(session):
    before = signal.getsignal(signal.SIGHUP)
    with pytest.raises(tunnel_mod.TunnelHangup):
        with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
            signal.raise_signal(signal.SIGHUP)
    assert session["signals"] == GROUP_STOP
    assert signal.getsignal(signal.SIGHUP) is before


def test_a_group_already_gone_is_not_an_error(session, monkeypatch):
    def gone(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(tunnel_mod.os, "killpg", gone)
    with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
        pass


def test_get_tunnel_reads_the_injection_variable(monkeypatch):
    monkeypatch.setenv("VECTEN_TUNNEL_RUNNER", f"{__name__}:FakeTunnel")
    assert isinstance(tunnel_mod.get_tunnel(), FakeTunnel)
    monkeypatch.delenv("VECTEN_TUNNEL_RUNNER")
    assert isinstance(tunnel_mod.get_tunnel(), tunnel_mod.SessionManagerTunnel)


# Against Postgres 16 with a master that is not a superuser


@pytest.mark.docker
@pytest.mark.skipif(not docker_util.docker_available(), reason="no docker daemon")
def test_admin_sql_as_a_non_superuser_master_is_idempotent(tmp_path):
    import psycopg

    from vecten_shared.sql import setup_sql_path

    init = tmp_path / "init"
    (init / "vecten").mkdir(parents=True)
    shutil.copy(setup_sql_path(), init / "vecten" / "warehouse_setup.sql")
    with docker_util.pg_container(init, {"POSTGRES_PASSWORD": "super"}) as (connect, name):
        with connect("postgres", "super", "postgres") as c:
            c.execute(f"create role {MASTER_USER} login createrole createdb password 'm'")
            c.execute("create role rds_superuser nologin")
            c.execute(f"grant rds_superuser to {MASTER_USER} with admin option")
            c.execute(f"alter database postgres owner to {MASTER_USER}")
            c.execute(f"alter database template1 owner to {MASTER_USER}")
            c.commit()
        setup = subprocess.run(
            ["docker", "exec", "-e", "PGPASSWORD=m", "-e", "LOADER_PASSWORD=l1", name, "psql"]
            + ["-h", "127.0.0.1", "-U", MASTER_USER, "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
            + ["-v", "with_airflow=false", "-v", "with_mcp_state=false"]
            + ["-v", "with_warehouse=true", "-v", "with_mcp=false"]
            + ["-f", "/docker-entrypoint-initdb.d/vecten/warehouse_setup.sql"],
            capture_output=True,
            text=True,
        )
        assert setup.returncode == 0, setup.stderr

        stored: list[str] = []
        for _ in range(2):
            with connect(MASTER_USER, "m", "postgres") as conn:
                admin.ensure_admin(
                    conn,
                    "admin_jane",
                    stored.append,
                    password_stored=bool(stored),
                    grants=AwsProvider.admin_grants,
                )
        assert len(stored) == 1
        # The secret went missing: the role exists, so its password is set anew.
        with connect(MASTER_USER, "m", "postgres") as conn:
            outcome = admin.ensure_admin(
                conn,
                "admin_jane",
                stored.append,
                password_stored=False,
                grants=AwsProvider.admin_grants,
            )
        assert outcome == admin.AdminOutcome(created=False, password_rotated=True)
        assert len(stored) == 2
        with pytest.raises(psycopg.OperationalError):
            connect("admin_jane", stored[0], "warehouse")
        with connect("admin_jane", stored[1], "warehouse") as c:
            row = c.execute(
                "select pg_has_role('admin_jane', 'rds_superuser', 'member'),"
                " has_database_privilege('admin_jane', 'warehouse', 'CONNECT')"
            ).fetchone()
        assert row == (True, True)
        with connect("postgres", "super", "postgres") as c:
            attrs = c.execute(
                "select rolcanlogin, rolcreaterole, rolcreatedb, rolsuper from pg_roles"
                " where rolname = 'admin_jane'"
            ).fetchone()
        assert attrs == (True, True, True, False)
        with pytest.raises(psycopg.OperationalError):
            connect("admin_jane", "wrong", "warehouse")


# SIGHUP around the session's lifetime


@pytest.fixture
def sighup_restored():
    before = signal.getsignal(signal.SIGHUP)
    yield before
    signal.signal(signal.SIGHUP, before)


def test_the_hangup_handler_is_in_place_before_the_session_starts(
    session, monkeypatch, sighup_restored
):
    seen = {}
    start = tunnel_mod.subprocess.Popen

    def popen(args, **kwargs):
        seen["handler"] = signal.getsignal(signal.SIGHUP)
        return start(args, **kwargs)

    monkeypatch.setattr(tunnel_mod.subprocess, "Popen", popen)
    with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
        pass
    assert seen["handler"] is tunnel_mod._hangup
    assert signal.getsignal(signal.SIGHUP) is sighup_restored


def test_an_ignored_sighup_stays_ignored(session, sighup_restored):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    assert session["signals"] == GROUP_STOP


def test_a_second_hangup_cannot_interrupt_the_group_kill(session, monkeypatch, sighup_restored):
    handlers = []

    def killpg(pgid, sig):
        handlers.append(signal.getsignal(signal.SIGHUP))
        # The terminal hangs up again while the group is being stopped.
        signal.raise_signal(signal.SIGHUP)
        session["signals"].append((pgid, sig))

    monkeypatch.setattr(tunnel_mod.os, "killpg", killpg)
    with pytest.raises(tunnel_mod.TunnelHangup):
        with tunnel_mod.SessionManagerTunnel().open("i-0mcp", "db.example", 5432, 15432, REGION):
            signal.raise_signal(signal.SIGHUP)
    assert handlers == [signal.SIG_IGN, signal.SIG_IGN]
    assert session["signals"] == GROUP_STOP
    assert signal.getsignal(signal.SIGHUP) is sighup_restored


# Scaleway: the warehouse's public endpoint, opened by database_public_cidrs


def _open_to(root: Path, cidrs: list[str]) -> None:
    path = root / "deploy" / "envs" / "prod.tfvars"
    path.write_text(path.read_text() + f"database_public_cidrs = {json.dumps(cidrs)}\n")


@pytest.fixture
def scaleway(tmp_path, monkeypatch):
    root = render_aws(tmp_path / "scw", cloud="scaleway")
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("VECTEN_TUNNEL_RUNNER", f"{__name__}:FakeTunnel")
    monkeypatch.setattr(FakeTunnel, "opened", [])
    org, storage, _ = install(monkeypatch, tmp_path)
    outputs = prepare_environment(
        root, org, storage, scaleway_outputs(), deployed_commit=COMMIT, public=True
    )
    return type("Scw", (), {"root": root, "org": org, "outputs": outputs})


def test_scaleway_creates_the_admin_over_the_public_endpoint(invoke, scaleway, fake_db):
    _open_to(scaleway.root, ["203.0.113.7/32"])
    r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    [kwargs] = fake_db["kwargs"]
    assert (kwargs["host"], kwargs["port"], kwargs["sslmode"]) == ("51.15.0.9", 20123, "verify-ca")
    assert kwargs["require_auth"] == "scram-sha-256"
    assert fake_db["certificates"] == [SCW_CERTIFICATE_PEM]
    assert not Path(kwargs["sslrootcert"]).exists()
    assert kwargs["user"] == SCW_MASTER_USER and kwargs["password"] == SCW_MASTER_PASSWORD
    assert FakeTunnel.opened == []
    [conn] = fake_db["connections"]
    assert "GRANT warehouse_owner, loader TO admin_jane" in conn.statements
    assert not any("rds_superuser" in s for s in conn.statements)
    password = scaleway.org.secret_value("/vecten/acme/prod/admin", "ADMIN_JANE_PASSWORD")
    assert password and _scram_matches(
        next(s for s in conn.statements if s.startswith("CREATE ROLE")).split("PASSWORD '")[1][:-1],
        password,
    )
    data = doc(r)
    assert data["secret_id"] == "/vecten/acme/prod/admin/ADMIN_JANE_PASSWORD"
    assert password not in r.stdout and SCW_MASTER_PASSWORD not in r.stdout


def test_scaleway_failed_connection_removes_the_certificate_file(invoke, scaleway, monkeypatch):
    _open_to(scaleway.root, ["203.0.113.7/32"])
    seen: list[Path] = []

    def connect(kwargs):
        seen.append(Path(kwargs["sslrootcert"]))
        assert seen[0].exists()
        raise RuntimeError("server closed the connection")

    monkeypatch.setattr(admin, "default_connect", connect)
    r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 5
    assert seen and not seen[0].exists()


def test_ensure_admin_takes_each_clouds_grants_and_assumes_none():
    with pytest.raises(TypeError):
        admin.ensure_admin(
            FakeConnection(set()), "admin_jane", lambda v: None, password_stored=False
        )
    conn = FakeConnection(set())
    admin.ensure_admin(
        conn, "admin_jane", lambda v: None, password_stored=False, grants=("a_role", "b_role")
    )
    assert "GRANT a_role, b_role TO admin_jane" in conn.statements


def test_aws_connections_require_scram_too(invoke, project, fake_db, monkeypatch):
    with cloud(project, monkeypatch):
        r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    [kwargs] = fake_db["kwargs"]
    assert kwargs["require_auth"] == "scram-sha-256"
    # The SSM tunnel ends at 127.0.0.1, which no RDS certificate names.
    assert kwargs["sslmode"] == "require"


def test_a_libpq_that_cannot_require_scram_is_a_prerequisite(monkeypatch):
    import psycopg

    monkeypatch.setattr(psycopg.pq, "version", lambda: 150004)
    with pytest.raises(Failure) as caught:
        admin.connect_with_psycopg({"host": "127.0.0.1"})
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "SCRAM" in caught.value.message


def test_the_bundled_libpq_reads_require_auth():
    import psycopg

    assert psycopg.pq.version() >= admin.REQUIRE_AUTH_LIBPQ
    assert "require_auth" in psycopg.conninfo.conninfo_to_dict(
        psycopg.conninfo.make_conninfo(require_auth="scram-sha-256")
    )


def test_scaleway_refuses_without_public_cidrs_before_any_cloud_call(invoke, scaleway, fake_db):
    r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 4
    error = doc(r)["error"]
    assert "database_public_cidrs" in error and "deploy/envs/prod.tfvars" in error
    assert scaleway.org.calls == []
    assert fake_db["connections"] == []


def test_scaleway_endpoint_not_open_yet_names_deploy(invoke, scaleway, fake_db):
    _open_to(scaleway.root, ["203.0.113.7/32"])
    for instance in scaleway.org.rdb_instances.values():
        instance["endpoints"] = [e for e in instance["endpoints"] if e["load_balancer"] is None]
    r = invoke("user", "add-admin", "jane", "--env", "prod", "--json")
    assert r.exit_code == 3
    assert "vecten deploy --env prod" in doc(r)["error"]
    assert fake_db["connections"] == []


@pytest.mark.docker
@pytest.mark.skipif(not docker_util.docker_available(), reason="no docker daemon")
def test_scaleway_admin_grants_need_no_superuser(tmp_path):
    """The roles a Scaleway admin gets are ones the master created, so it holds ADMIN on them
    (Postgres 16) whether or not the managed user is a superuser."""
    from vecten_shared.sql import setup_sql_path

    init = tmp_path / "init"
    (init / "vecten").mkdir(parents=True)
    shutil.copy(setup_sql_path(), init / "vecten" / "warehouse_setup.sql")
    with docker_util.pg_container(init, {"POSTGRES_PASSWORD": "super"}) as (connect, name):
        with connect("postgres", "super", "postgres") as c:
            c.execute(f"create role {MASTER_USER} login createrole createdb password 'm'")
            c.execute(f"alter database postgres owner to {MASTER_USER}")
            c.execute(f"alter database template1 owner to {MASTER_USER}")
            c.commit()
        setup = subprocess.run(
            ["docker", "exec", "-e", "PGPASSWORD=m", "-e", "LOADER_PASSWORD=l1", name, "psql"]
            + ["-h", "127.0.0.1", "-U", MASTER_USER, "-d", "postgres", "-v", "ON_ERROR_STOP=1"]
            + ["-v", "with_airflow=false", "-v", "with_mcp_state=false"]
            + ["-v", "with_warehouse=true", "-v", "with_mcp=false"]
            + ["-f", "/docker-entrypoint-initdb.d/vecten/warehouse_setup.sql"],
            capture_output=True,
            text=True,
        )
        assert setup.returncode == 0, setup.stderr
        with connect("loader", "l1", "warehouse") as c:
            c.execute("create table marts.orders (id int)")
            c.commit()
        stored: list[str] = []
        with connect(MASTER_USER, "m", "postgres") as conn:
            admin.ensure_admin(
                conn,
                "admin_jane",
                stored.append,
                password_stored=False,
                grants=("warehouse_owner", "loader"),
            )
        with connect("admin_jane", stored[0], "warehouse") as c:
            c.execute("select count(*) from marts.orders").fetchone()
            c.execute("create schema scratch")
