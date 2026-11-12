import ast
import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import vecten.deploy.vm
from vecten.deploy import admin
from vecten_shared.provider import scope_for
from vecten_shared.sql import setup_sql_path, setup_variables
from vecten_shared.stack_secrets import FIXED
from vecten_shared.versions import VERSIONS

from . import docker_util

APPLY = Path(vecten.deploy.vm.__file__).parent / "apply.py"
PREFIX = "vecten/acme/prod/"
DOMAIN = "data.acme.example"


def load_agent():
    spec = importlib.util.spec_from_file_location("vecten_vm_apply_under_test", APPLY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def agent(tmp_path, monkeypatch):
    module = load_agent()
    monkeypatch.setattr(module, "LETSENCRYPT", tmp_path / "letsencrypt")
    monkeypatch.setattr(module, "RUN_DIR", tmp_path / "run" / "vecten")
    return module


def pg(host, databases, key):
    return {
        "host": host,
        "port": 5432,
        "databases": databases,
        "master_secret": f"arn:aws:secretsmanager:eu-central-1:123:secret:rds!{key}-abc",
    }


def outputs(shape):
    hosts = {
        "standard": {
            "platform": (["platform"], [f"airflow.{DOMAIN}"]),
            "mcp": (["mcp"], [f"mcp.{DOMAIN}"]),
        },
        "platform_only": {"platform": (["platform"], [f"airflow.{DOMAIN}"])},
        "minimal": {"main": (["platform", "mcp"], [f"airflow.{DOMAIN}", f"mcp.{DOMAIN}"])},
        "mcp_only": {"mcp": (["mcp"], [f"mcp.{DOMAIN}"])},
    }[shape]
    postgres = {
        "standard": {
            "state": pg("state.rds", ["airflow", "mcp_state"], "state"),
            "warehouse": pg("wh.rds", ["warehouse"], "warehouse"),
        },
        "platform_only": {
            "state": pg("state.rds", ["airflow"], "state"),
            "warehouse": pg("wh.rds", ["warehouse"], "warehouse"),
        },
        "minimal": {"state": pg("state.rds", ["airflow", "mcp_state", "warehouse"], "state")},
        "mcp_only": {"state": pg("state.rds", ["mcp_state"], "state")},
    }[shape]
    setup = {"standard": "mcp", "platform_only": "platform", "minimal": "main", "mcp_only": "mcp"}
    return {
        "environment": "prod",
        "region": "eu-central-1",
        "hosts": {
            name: {
                "instance_id": f"i-{name}",
                "public_ip": "203.0.113.7",
                "components": components,
                "hostnames": names,
            }
            for name, (components, names) in hosts.items()
        },
        "setup_host": setup[shape],
        "bucket": "vecten-acme-prod-artifacts",
        "secret_prefix": PREFIX,
        "postgres": postgres,
    }


def host_doc(out, name, commit="c1", connector_secrets=(), **services):
    h = out["hosts"][name]
    return {
        "host": name,
        "cloud": "aws",
        "project_id": "",
        "bucket": out["bucket"],
        "plugin_dir": "/usr/libexec/docker/cli-plugins",
        "components": h["components"],
        "env": "prod",
        "project": "acme",
        "region": out["region"],
        "commit": commit,
        "setup_host": out["setup_host"] == name,
        "hostnames": h["hostnames"],
        "secret_prefix": out["secret_prefix"],
        "compose_project": "vecten-acme-prod",
        "connector_secrets": list(connector_secrets),
        "compose_plugin_version": VERSIONS.docker_compose_plugin,
        "buildx_plugin_version": VERSIONS.docker_buildx_plugin,
        "certbot_image": VERSIONS.certbot_image,
        "postgres_image": f"postgres:{VERSIONS.postgres_major}",
        "airflow_parallelism": 16,
        "mcp_pool_cap": 20,
        **services,
    }


def stage(releases, out, name, commit="c1", **kw):
    release = releases / commit
    ship = release / ".vecten" / "ship"
    ship.mkdir(parents=True)
    (ship / "host.json").write_text(json.dumps(host_doc(out, name, commit, **kw)))
    (ship / "warehouse_setup.sql").write_text(setup_sql_path().read_text())
    envs = release / ".vecten" / "environments"
    envs.mkdir(parents=True)
    (envs / "prod.json").write_text(json.dumps(out))
    (release / ".vecten" / "stack").mkdir(parents=True)
    return release


class FakeRun:
    """Answers the commands the agent shells out to and records every call."""

    def __init__(
        self,
        store=None,
        compose_version=None,
        certbot_fails=False,
        nginx_ids="",
        buildx_version=None,
        containers=(),
        logs=None,
    ):
        self.store = dict(store or {})
        self.calls = []
        self.compose_version = compose_version or VERSIONS.docker_compose_plugin
        self.buildx_version = buildx_version or VERSIONS.docker_buildx_plugin
        self.certbot_fails = certbot_fails
        self.nginx_ids = nginx_ids
        # (id, service, state, status) rows `docker ps --format` answers, and each id's
        # (stdout, stderr) from `docker logs`.
        self.containers = list(containers)
        self.logs = dict(logs or {})
        self.ps_fails = False
        self.downloads = {}
        self.rules = set()
        self.iptables_fails = False

    def __call__(self, args, env=None, input=None):
        self.calls.append({"args": list(args), "env": dict(env or {}), "input": input})
        ok = subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "aws":
            return self._aws(args, input)
        if args[0] == "iptables":
            return self._iptables(args)
        if args[:3] == ["docker", "compose", "version"]:
            return subprocess.CompletedProcess(args, 0, f"{self.compose_version}\n", "")
        if args[:3] == ["docker", "buildx", "version"]:
            out = f"github.com/docker/buildx v{self.buildx_version} 0123abc\n"
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[:2] == ["docker", "ps"] and "--format" in args:
            if self.ps_fails:
                return subprocess.CompletedProcess(args, 1, "", "daemon gone")
            rows = "".join("\t".join(row) + "\n" for row in self.containers)
            return subprocess.CompletedProcess(args, 0, rows, "")
        if args[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(args, 0, self.nginx_ids, "")
        if args[:2] == ["docker", "logs"]:
            out, err = self.logs.get(args[-1], ("", ""))
            return subprocess.CompletedProcess(args, 0, out, err)
        if args[:2] == ["docker", "run"] and "certonly" in args and self.certbot_fails:
            return subprocess.CompletedProcess(args, 1, "", "dns not ready")
        if args[0] == "curl":
            dest = Path(args[args.index("-o") + 1])
            url = args[-1]
            dest.write_bytes(self.downloads.get(url, b""))
        return ok

    def _iptables(self, args):
        assert args[1:3] == ["-w", "30"], args
        rule = tuple(args[4:])
        if args[3] == "-C":
            return subprocess.CompletedProcess(args, 0 if rule in self.rules else 1, "", "")
        if self.iptables_fails:
            return subprocess.CompletedProcess(args, 4, "", "can't initialize iptables table")
        self.rules.add(rule)
        return subprocess.CompletedProcess(args, 0, "", "")

    def _aws(self, args, input):
        op = args[args.index("secretsmanager") + 1]
        if op == "get-secret-value":
            sid = args[args.index("--secret-id") + 1]
            if sid in self.store:
                return subprocess.CompletedProcess(args, 0, self.store[sid] + "\n", "")
            return subprocess.CompletedProcess(
                args, 254, "", "An error occurred (ResourceNotFoundException) when calling"
            )
        if op == "create-secret":
            sid = args[args.index("--name") + 1]
            assert args[args.index("--secret-string") + 1] == "file:///dev/stdin"
            self.store[sid] = input
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if op == "put-secret-value":
            sid = args[args.index("--secret-id") + 1]
            self.store[sid] = input
            return subprocess.CompletedProcess(args, 0, "{}", "")
        raise AssertionError(f"unexpected aws call {args}")

    def argv(self, *prefix):
        return [c["args"] for c in self.calls if c["args"][: len(prefix)] == list(prefix)]

    def created(self):
        return [
            c["args"][c["args"].index("--name") + 1]
            for c in self.calls
            if "create-secret" in c["args"]
        ]


def master(out, key):
    return {
        out["postgres"][key]["master_secret"]: json.dumps(
            {"username": "vecten_master", "password": f"master-{key}-pw"}
        )
    }


def seeded(out, extra=()):
    """A store where every secret the shape needs already exists."""
    store = {}
    for key in out["postgres"]:
        store |= master(out, key)
    names = [
        "AIRFLOW_DB_PASSWORD",
        "LOADER_PASSWORD",
        "MCP_STATE_DB_PASSWORD",
        "MCP_PROVISIONER_PASSWORD",
        "MCP_IMPERSONATOR_PASSWORD",
        "AIRFLOW_FERNET_KEY",
        "AIRFLOW_API_SECRET_KEY",
        "AIRFLOW_JWT_SECRET",
        "AIRFLOW_ADMIN_PASSWORD",
        "AIRFLOW_BASIC_AUTH_PASSWORD",
        *extra,
    ]
    for n in names:
        scope = "mcp" if n.startswith("MCP_") else "platform"
        store[f"{PREFIX}{scope}/{n}"] = f"value-of-{n}"
    return store


def with_certs(agent, hostnames):
    for h in hostnames:
        live = agent.LETSENCRYPT / "live" / h
        live.mkdir(parents=True, exist_ok=True)
        (live / "fullchain.pem").write_text("cert")


PUBLIC_IP = "203.0.113.7"


def resolves_here(hostname):
    return [PUBLIC_IP]


def ship(agent, release, run, tmp_path):
    with_certs(agent, json.loads((release / ".vecten/ship/host.json").read_text())["hostnames"])
    agent.ship(release, run, releases=release.parent, sleep=lambda s: None, resolve=resolves_here)


def test_agent_parses_as_python_3_9_and_imports_only_the_stdlib():
    source = APPLY.read_text()
    tree = ast.parse(source, feature_version=(3, 9))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.add(node.module.split(".")[0])
    assert modules, "the agent imports nothing?"
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules


def test_agent_scope_rule_matches_the_provider():
    agent = load_agent()
    for name in (
        "AIRFLOW_DB_PASSWORD",
        "LOADER_PASSWORD",
        "MCP_STATE_DB_PASSWORD",
        "MCP_PROVISIONER_PASSWORD",
        "MCP_IMPERSONATOR_PASSWORD",
        "AIRFLOW_FERNET_KEY",
        "HUBSPOT_API_KEY",
        "ALERT_WEBHOOK_URL",
        "DEPLOY_SNOWFLAKE_USER",
        "ADMIN_ANNA_PASSWORD",
        "SETUP_STATE_ADMIN_PASSWORD",
    ):
        assert agent.scope_for(name) == scope_for(name), name


@pytest.mark.parametrize(
    "databases",
    [["airflow"], ["airflow", "mcp_state"], ["warehouse"], ["airflow", "mcp_state", "warehouse"]],
)
@pytest.mark.parametrize("with_mcp", [True, False])
def test_agent_setup_flags_match_the_shared_rule(databases, with_mcp):
    agent = load_agent()
    assert agent.setup_flags(databases, with_mcp) == setup_variables(databases, with_mcp)


def test_admin_users_match_the_local_stack():
    agent = load_agent()
    assert agent.AIRFLOW_ADMIN_USER == FIXED["AIRFLOW_ADMIN_USER"]


def test_load_host_reads_the_contract_and_names_missing_keys(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform")
    host = agent.load_host(release)
    assert host["host"] == "platform" and host["release"] == str(release)
    doc = json.loads((release / ".vecten/ship/host.json").read_text())
    del doc["certbot_image"]
    (release / ".vecten/ship/host.json").write_text(json.dumps(doc))
    with pytest.raises(agent.AgentError, match="certbot_image"):
        agent.load_host(release)


def test_secret_values_never_in_argv(agent, tmp_path):
    out = outputs("minimal")
    release = stage(tmp_path / "releases", out, "main", connector_secrets=["HUBSPOT_API_KEY"])
    run = FakeRun(store={k: v for k, v in master(out, "state").items()})
    run.store[f"{PREFIX}platform/HUBSPOT_API_KEY"] = "hubspot-secret-value"
    ship(agent, release, run, tmp_path)
    values = [v for v in run.store.values()] + ["master-state-pw"]
    assert len(run.created()) == 10
    for call in run.calls:
        for arg in call["args"]:
            for value in values:
                assert value not in arg, (value, call["args"])


def test_generated_once(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "mcp")
    run = FakeRun(store=master(out, "state") | master(out, "warehouse"))
    ship(agent, release, run, tmp_path)
    assert sorted(run.created()) == sorted(
        [
            f"{PREFIX}platform/AIRFLOW_DB_PASSWORD",
            f"{PREFIX}platform/LOADER_PASSWORD",
            f"{PREFIX}mcp/MCP_STATE_DB_PASSWORD",
            f"{PREFIX}mcp/MCP_PROVISIONER_PASSWORD",
            f"{PREFIX}mcp/MCP_IMPERSONATOR_PASSWORD",
        ]
    )
    for sid in run.created():
        assert len(run.store[sid]) >= 43  # 32 random bytes, url-safe base64
    first = dict(run.store)
    again = FakeRun(store=first)
    release2 = stage(tmp_path / "releases", out, "mcp", commit="c2")
    ship(agent, release2, again, tmp_path)
    assert again.created() == [] and again.argv("aws", "--region") and again.store == first


def test_generated_names_follow_the_configuration(agent):
    gen = agent.generated_names
    assert gen(outputs("platform_only")) == [
        "AIRFLOW_DB_PASSWORD",
        "LOADER_PASSWORD",
    ]
    assert gen(outputs("mcp_only")) == ["MCP_STATE_DB_PASSWORD"]
    assert (
        set(gen(outputs("minimal"))) == set(gen(outputs("standard")))
        and len(gen(outputs("minimal"))) == 5
    )


def test_existing_secret_without_a_value_gets_put_not_created_twice(agent, tmp_path):
    out = outputs("mcp_only")
    host = agent.load_host(stage(tmp_path / "releases", out, "mcp"))
    run = FakeRun(store=master(out, "state"))

    def answer(args, env=None, input=None):
        if "create-secret" in args:
            run.calls.append({"args": list(args), "env": {}, "input": input})
            return subprocess.CompletedProcess(args, 254, "", "(ResourceExistsException)")
        return run(args, env, input)

    agent.ensure_generated(host, out, answer)
    puts = run.argv("aws", "--region", "eu-central-1", "secretsmanager", "put-secret-value")
    assert len(puts) == 1 and puts[0][puts[0].index("--secret-id") + 1].endswith(
        "mcp/MCP_STATE_DB_PASSWORD"
    )


def test_an_airflow_app_secret_without_a_value_is_named_never_put(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "platform"))
    run = FakeRun(store=seeded(out))
    del run.store[f"{PREFIX}platform/AIRFLOW_JWT_SECRET"]

    def answer(args, env=None, input=None):
        if "create-secret" in args:
            run.calls.append({"args": list(args), "env": {}, "input": input})
            return subprocess.CompletedProcess(args, 254, "", "(ResourceExistsException)")
        return run(args, env, input)

    with pytest.raises(agent.AgentError) as exc:
        agent.ensure_generated(host, out, answer)
    message = str(exc.value)
    assert f"{PREFIX}platform/AIRFLOW_JWT_SECRET" in message and "without a value" in message
    assert run.argv("aws", "--region", "eu-central-1", "secretsmanager", "put-secret-value") == []


def test_platform_host_generates_its_airflow_app_secrets(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "platform"))
    run = FakeRun(store=seeded(out))
    for n in ("AIRFLOW_FERNET_KEY", "AIRFLOW_ADMIN_PASSWORD"):
        del run.store[f"{PREFIX}platform/{n}"]
    agent.ensure_generated(host, out, run)
    assert sorted(run.created()) == [
        f"{PREFIX}platform/AIRFLOW_ADMIN_PASSWORD",
        f"{PREFIX}platform/AIRFLOW_FERNET_KEY",
    ]
    fernet = base64.urlsafe_b64decode(run.store[f"{PREFIX}platform/AIRFLOW_FERNET_KEY"])
    assert len(fernet) == 32


def test_non_setup_platform_host_never_reads_a_master_secret(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform")
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    ids = [a[a.index("--secret-id") + 1] for a in run.argv("aws") if "--secret-id" in a]
    assert ids and not any(i.startswith("arn:") for i in ids)
    assert not [a for a in run.argv("docker", "run") if "psql" in a]
    assert not any(f"{PREFIX}mcp/" in i for i in ids)
    assert run.created() == []


def test_secret_env_for_the_platform_host(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(
        stage(
            tmp_path / "releases",
            out,
            "platform",
            connector_secrets=["HUBSPOT_API_KEY", "STRIPE_KEY"],
        )
    )
    store = seeded(out, extra=["HUBSPOT_API_KEY"])
    store[f"{PREFIX}platform/AIRFLOW_DB_PASSWORD"] = "p@ss/word"
    env = agent.secret_env(host, out, FakeRun(store=store))
    assert env["AIRFLOW_DATABASE_URL"] == (
        "postgresql+psycopg2://airflow:p%40ss%2Fword@state.rds:5432/airflow?sslmode=require"
    )
    assert env["LOADER_PASSWORD"] == "value-of-LOADER_PASSWORD"
    assert env["HUBSPOT_API_KEY"] == "value-of-HUBSPOT_API_KEY"
    # An absent connector secret or webhook is left out; the run names it.
    assert "STRIPE_KEY" not in env and "ALERT_WEBHOOK_URL" not in env
    assert "MCP_STATE_DB_PASSWORD" not in env
    for n in ("AIRFLOW_FERNET_KEY", "AIRFLOW_API_SECRET_KEY", "AIRFLOW_JWT_SECRET"):
        assert env[n] == f"value-of-{n}"


def test_secret_env_refuses_a_missing_required_secret_naming_it(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "mcp"))
    with pytest.raises(agent.AgentError, match="mcp/MCP_STATE_DB_PASSWORD"):
        agent.secret_env(host, out, FakeRun(store={}))


def test_secret_read_failure_names_the_secret_not_the_output(agent, tmp_path):
    out = outputs("mcp_only")
    host = agent.load_host(stage(tmp_path / "releases", out, "mcp"))

    def denied(args, env=None, input=None):
        return subprocess.CompletedProcess(args, 254, "", "AccessDeniedException secret-ish")

    with pytest.raises(agent.AgentError) as exc:
        agent.secret_env(host, out, denied)
    assert "MCP_STATE_DB_PASSWORD" in str(exc.value) and "secret-ish" not in str(exc.value)


def psql_runs(run):
    return [c for c in run.calls if c["args"][:2] == ["docker", "run"] and "psql" in c["args"]]


def flags(args):
    return {
        a.split("=", 1)[0]: a.split("=", 1)[1]
        for i, a in enumerate(args)
        if i and args[i - 1] == "-v" and a.startswith("with_")
    }


def test_setup_runs_once_per_instance_with_standard_flags(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "mcp")
    run = FakeRun(store=seeded(out))
    host = agent.load_host(release)
    agent.run_setup(host, out, run)
    runs = {c["args"][c["args"].index("-h") + 1]: c for c in psql_runs(run)}
    assert set(runs) == {"state.rds", "wh.rds"}
    assert flags(runs["state.rds"]["args"]) == {
        "with_airflow": "true",
        "with_mcp_state": "true",
        "with_warehouse": "false",
        "with_mcp": "false",
    }
    assert flags(runs["wh.rds"]["args"]) == {
        "with_airflow": "false",
        "with_mcp_state": "false",
        "with_warehouse": "true",
        "with_mcp": "true",
    }
    wh = runs["wh.rds"]
    assert wh["env"]["PGPASSWORD"] == "master-warehouse-pw"
    assert wh["env"]["PGSSLMODE"] == "require"
    assert wh["env"]["MASTER_ROLE"] == "vecten_master"
    assert verifies(wh["env"]["LOADER_PASSWORD"], "value-of-LOADER_PASSWORD")
    assert "WAREHOUSE_DB_PASSWORD" not in wh["env"]
    assert verifies(wh["env"]["PROVISIONER_PASSWORD"], "value-of-MCP_PROVISIONER_PASSWORD")
    assert verifies(wh["env"]["IMPERSONATOR_PASSWORD"], "value-of-MCP_IMPERSONATOR_PASSWORD")
    # Only the master's own password reaches the server as plaintext.
    plain = [v for k, v in wh["env"].items() if "value-of-" in v]
    assert plain == []
    assert "AIRFLOW_DB_PASSWORD" not in wh["env"]
    args = wh["args"]
    for name in wh["env"]:
        assert args[args.index(name) - 1] == "-e", name
    assert f"{release}/.vecten/ship:/sql:ro" in args
    assert args[args.index("-U") + 1] == "vecten_master"
    assert args[args.index("-d") + 1] == "postgres"
    assert args[-2:] == ["-f", "/sql/warehouse_setup.sql"]
    assert "ON_ERROR_STOP=1" in args and "--network" in args
    assert f"postgres:{VERSIONS.postgres_major}" in args
    state = runs["state.rds"]["env"]
    assert verifies(state["AIRFLOW_DB_PASSWORD"], "value-of-AIRFLOW_DB_PASSWORD")
    assert verifies(state["MCP_STATE_DB_PASSWORD"], "value-of-MCP_STATE_DB_PASSWORD")


SCRAM = re.compile(r"SCRAM-SHA-256\$4096:([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)")


def verifies(verifier: str, password: str) -> bool:
    """True when `verifier` is the SCRAM-SHA-256 verifier of `password` under its own salt."""
    match = SCRAM.fullmatch(verifier)
    if match is None:
        return False
    salt = base64.b64decode(match.group(1))
    return admin.scram_verifier(password, salt) == verifier


def test_agent_scram_verifier_matches_add_admin(agent):
    salt = bytes(range(16))
    for password in ("value-of-LOADER_PASSWORD", "Zx_9-long-generated-token"):
        assert agent.scram_verifier(password, salt) == admin.scram_verifier(password, salt)
    fresh = agent.scram_verifier("p")
    match = SCRAM.fullmatch(fresh)
    assert match is not None and len(base64.b64decode(match.group(1))) == 16
    assert agent.scram_verifier("p") != fresh


def test_setup_under_minimal_is_one_run_with_every_flag(agent, tmp_path):
    out = outputs("minimal")
    host = agent.load_host(stage(tmp_path / "releases", out, "main"))
    run = FakeRun(store=seeded(out))
    agent.run_setup(host, out, run)
    (only,) = psql_runs(run)
    assert set(flags(only["args"]).values()) == {"true"}


def test_setup_failure_is_named_without_psql_output(agent, tmp_path):
    out = outputs("mcp_only")
    host = agent.load_host(stage(tmp_path / "releases", out, "mcp"))
    run = FakeRun(store=seeded(out))

    def failing(args, env=None, input=None):
        if "psql" in args:
            return subprocess.CompletedProcess(args, 3, "", "ERROR near 'value-of-x'")
        return run(args, env, input)

    with pytest.raises(agent.AgentError) as exc:
        agent.run_setup(host, out, failing)
    assert "state" in str(exc.value) and "value-of" not in str(exc.value)


def test_missing_certificate_stops_nginx_then_runs_certbot(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "platform"))
    run = FakeRun(nginx_ids="abc123\n")
    agent.ensure_certificates(host, out, run, lambda s: None, resolves_here)
    names = [c["args"][:2] for c in run.calls]
    assert names.index(["docker", "stop"]) < names.index(["docker", "run"])
    assert run.argv("docker", "stop") == [["docker", "stop", "abc123"]]
    (certbot,) = run.argv("docker", "run")
    assert VERSIONS.certbot_image in certbot
    i = certbot.index("certonly")
    assert certbot[i:] == [
        "certonly",
        "--standalone",
        "--non-interactive",
        "--agree-tos",
        "--register-unsafely-without-email",
        "-d",
        f"airflow.{DOMAIN}",
    ]
    assert "80:80" in certbot
    ps = run.argv("docker", "ps")[0]
    assert "label=com.docker.compose.project=vecten-acme-prod" in ps


def test_present_certificate_runs_no_certbot(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "platform"))
    with_certs(agent, host["hostnames"])
    run = FakeRun(nginx_ids="abc\n")

    def never(hostname):
        raise AssertionError("no lookup for a present certificate")

    agent.ensure_certificates(host, out, run, lambda s: None, never)
    assert run.calls == []


def certbot_calls(run):
    return [a for a in run.argv("docker", "run") if "certonly" in a]


def test_certbot_waits_for_dns_to_point_here_then_runs_once(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "platform"))
    answers = [[], ["198.51.100.9"], [PUBLIC_IP]]
    lookups = []

    def resolve(hostname):
        lookups.append(hostname)
        return answers.pop(0)

    slept = []
    run = FakeRun()
    agent.ensure_certificates(host, out, run, slept.append, resolve)
    assert lookups == [f"airflow.{DOMAIN}"] * 3
    assert slept == [15, 15]
    assert len(certbot_calls(run)) == 1


def test_dns_never_pointing_here_fails_without_calling_certbot(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "platform"))
    run = FakeRun(nginx_ids="abc\n")
    slept = []
    with pytest.raises(agent.AgentError) as exc:
        agent.ensure_certificates(host, out, run, slept.append, lambda h: ["198.51.100.9"])
    message = str(exc.value)
    assert f"airflow.{DOMAIN}" in message and "198.51.100.9" in message and PUBLIC_IP in message
    assert sum(slept) >= 300 and all(s == 15 for s in slept)
    assert certbot_calls(run) == [] and run.argv("docker", "stop") == []


def test_a_hostname_that_does_not_resolve_is_named(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "platform"))
    run = FakeRun()
    with pytest.raises(agent.AgentError, match="does not resolve"):
        agent.ensure_certificates(host, out, run, lambda s: None, lambda h: [])
    assert certbot_calls(run) == []


def test_certbot_failing_makes_three_attempts_and_restarts_nginx(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, "platform"))
    run = FakeRun(certbot_fails=True, nginx_ids="abc\n")
    slept = []
    with pytest.raises(agent.AgentError, match=f"airflow.{DOMAIN}"):
        agent.ensure_certificates(host, out, run, slept.append, resolves_here)
    assert len(certbot_calls(run)) == 3
    assert slept == [30, 30]
    assert run.argv("docker", "start") == [["docker", "start", "abc"]]
    names = [c["args"][:2] for c in run.calls]
    assert names.index(["docker", "start"]) > max(
        i for i, n in enumerate(names) if n == ["docker", "run"]
    )


def test_resolver_returns_the_addresses_a_name_has(agent):
    assert "127.0.0.1" in agent.resolve("localhost")
    assert agent.resolve("no-such-host.invalid") == []


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_write_run_files_formats_and_modes(agent, tmp_path):
    host = {"components": ["platform"]}
    env = {"AIRFLOW_ADMIN_PASSWORD": "adm1n", "AIRFLOW_BASIC_AUTH_PASSWORD": "b4sic"}
    run_dir = tmp_path / "run" / "vecten"
    agent.write_run_files(host, env, run_dir)
    assert mode(run_dir) == 0o700
    passwords = run_dir / "airflow_passwords.json"
    assert json.loads(passwords.read_text()) == {"admin": "adm1n"}
    digest = base64.b64encode(hashlib.sha1(b"b4sic").digest()).decode()
    assert (run_dir / "htpasswd").read_text() == f"admin:{{SHA}}{digest}\n"
    assert mode(passwords) == 0o600 and mode(run_dir / "htpasswd") == 0o600


def test_write_run_files_replaces_a_directory_docker_created(agent, tmp_path):
    run_dir = tmp_path / "run" / "vecten"
    (run_dir / "airflow_passwords.json").mkdir(parents=True)
    (run_dir / "htpasswd").mkdir()
    env = {"AIRFLOW_ADMIN_PASSWORD": "a", "AIRFLOW_BASIC_AUTH_PASSWORD": "b"}
    agent.write_run_files({"components": ["platform"]}, env, run_dir)
    assert (run_dir / "airflow_passwords.json").is_file()
    assert (run_dir / "htpasswd").is_file()


def test_write_run_files_keeps_the_inode_a_bind_mount_holds(agent, tmp_path):
    run_dir = tmp_path / "run" / "vecten"
    env = {"AIRFLOW_ADMIN_PASSWORD": "a", "AIRFLOW_BASIC_AUTH_PASSWORD": "b"}
    agent.write_run_files({"components": ["platform"]}, env, run_dir)
    inode = os.stat(run_dir / "htpasswd").st_ino
    env["AIRFLOW_BASIC_AUTH_PASSWORD"] = "rotated"
    agent.write_run_files({"components": ["platform"]}, env, run_dir)
    assert os.stat(run_dir / "htpasswd").st_ino == inode


def test_run_files_go_to_the_container_users_when_root(agent, tmp_path, monkeypatch):
    owners = {}
    monkeypatch.setattr(agent.os, "geteuid", lambda: 0)
    monkeypatch.setattr(agent.os, "chown", lambda p, u, g: owners.__setitem__(Path(p).name, (u, g)))
    env = {"AIRFLOW_ADMIN_PASSWORD": "a", "AIRFLOW_BASIC_AUTH_PASSWORD": "b"}
    agent.write_run_files({"components": ["platform"]}, env, tmp_path / "run")
    assert owners == {"airflow_passwords.json": (50000, 0), "htpasswd": (101, 101)}


def test_mcp_only_host_writes_no_airflow_files(agent, tmp_path):
    run_dir = tmp_path / "run" / "vecten"
    agent.write_run_files({"components": ["mcp"]}, {}, run_dir)
    assert run_dir.is_dir() and list(run_dir.iterdir()) == []


def make_release(releases, commit):
    (releases / commit).mkdir(parents=True)
    return releases / commit


def test_swap_keeps_exactly_two_releases(agent, tmp_path):
    releases = tmp_path / "releases"
    for commit in ("a", "b", "c"):
        agent.swap(releases, make_release(releases, commit))
    assert os.readlink(releases / "current") == str((releases / "c").resolve())
    assert os.readlink(releases / "previous") == str((releases / "b").resolve())
    assert sorted(p.name for p in releases.iterdir()) == ["b", "c", "current", "previous"]


def test_swap_of_the_current_release_keeps_previous(agent, tmp_path):
    releases = tmp_path / "releases"
    agent.swap(releases, make_release(releases, "a"))
    agent.swap(releases, make_release(releases, "b"))
    agent.swap(releases, releases / "b")
    assert Path(os.readlink(releases / "previous")).name == "a"
    assert Path(os.readlink(releases / "current")).name == "b"


def compose_calls(run):
    return [c for c in run.calls if c["args"][:2] == ["docker", "compose"] and "up" in c["args"]]


def test_compose_up_uses_the_host_profiles_and_passes_secrets_by_env(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform")
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    (up,) = compose_calls(run)
    args = up["args"]
    stack = str(release.resolve() / ".vecten" / "stack")
    assert args[:6] == [
        "docker",
        "compose",
        "--project-name",
        "vecten-acme-prod",
        "--project-directory",
        stack,
    ]
    assert args[args.index("-f") + 1] == f"{stack}/docker-compose.yml"
    assert [args[i + 1] for i, a in enumerate(args) if a == "--profile"] == ["platform"]
    assert args[-4:] == ["up", "-d", "--build", "--remove-orphans"]
    env = up["env"]
    assert env["AIRFLOW_FERNET_KEY"] == "value-of-AIRFLOW_FERNET_KEY"
    assert env["AIRFLOW_ADMIN_USER"] == "admin"
    assert env["VECTEN_PROVIDER"] == "aws" and env["VECTEN_ENV"] == "prod"
    assert env["AWS_REGION"] == "eu-central-1"
    assert env["VECTEN_BUNDLE_COMMIT"] == "c1"
    assert env["VECTEN_ARTIFACT_URL"] == "s3://vecten-acme-prod-artifacts/platform/"
    assert env["AIRFLOW_REMOTE_BASE_LOG_FOLDER"] == "s3://vecten-acme-prod-artifacts/platform/logs"
    assert env["AIRFLOW_PARALLELISM"] == "16" and env["VECTEN_MCP_POOL_CAP"] == "20"
    assert os.readlink(release.parent / "current") == str(release.resolve())


def test_minimal_host_starts_both_profiles(agent, tmp_path):
    out = outputs("minimal")
    release = stage(tmp_path / "releases", out, "main")
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    (up,) = compose_calls(run)
    args = up["args"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--profile"] == ["platform", "mcp"]
    assert up["env"]["MCP_STATE_DB_PASSWORD"] == "value-of-MCP_STATE_DB_PASSWORD"


def test_ship_order_setup_before_compose(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "mcp")
    run = FakeRun(store=master(out, "state") | master(out, "warehouse"))
    ship(agent, release, run, tmp_path)
    kinds = []
    for c in run.calls:
        if "create-secret" in c["args"]:
            kinds.append("generate")
        elif "psql" in c["args"]:
            kinds.append("setup")
        elif c in compose_calls(run):
            kinds.append("compose")
    assert kinds.index("setup") > max(i for i, k in enumerate(kinds) if k == "generate")
    assert kinds[-1] == "compose"
    assert (agent.RUN_DIR).is_dir()


def test_rollback_restores_previous(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = stage(releases, out, "platform", commit="c1")
    run = FakeRun(store=seeded(out))
    ship(agent, first, run, tmp_path)
    ship(agent, stage(releases, out, "platform", commit="c2"), run, tmp_path)
    run.calls.clear()
    agent.rollback(releases, run)
    assert os.readlink(releases / "current") == str(first.resolve())
    assert not (releases / "previous").exists()
    (up,) = compose_calls(run)
    assert str(first.resolve() / ".vecten" / "stack") in up["args"]
    assert "--force-recreate" in up["args"]
    assert up["env"]["VECTEN_BUNDLE_COMMIT"] == "c1"
    assert (agent.RUN_DIR / "htpasswd").is_file()


def test_rollback_without_previous_fails(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    ship(agent, stage(releases, out, "platform"), FakeRun(store=seeded(out)), tmp_path)
    with pytest.raises(agent.AgentError, match="no previous bundle"):
        agent.rollback(releases, FakeRun(store=seeded(out)))


def test_boot_rebuilds_run_files_from_current_release(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    release = stage(releases, out, "platform")
    ship(agent, release, FakeRun(store=seeded(out)), tmp_path)
    run_dir = agent.RUN_DIR
    for child in run_dir.iterdir():
        child.unlink()
    run_dir.rmdir()
    # Docker restarts containers before the boot unit and creates a directory for a missing
    # bind-mount source.
    (run_dir / "airflow_passwords.json").mkdir(parents=True)
    run = FakeRun(store=seeded(out))
    agent.boot(releases, run)
    assert mode(run_dir / "airflow_passwords.json") == 0o600
    assert mode(run_dir / "htpasswd") == 0o600
    assert json.loads((run_dir / "airflow_passwords.json").read_text()) == {
        "admin": "value-of-AIRFLOW_ADMIN_PASSWORD"
    }
    (up,) = compose_calls(run)
    assert "--force-recreate" in up["args"]
    assert str(release.resolve() / ".vecten" / "stack") in up["args"]
    assert run.created() == [] and not psql_runs(run) and not run.argv("docker", "run")


def test_boot_without_a_current_release_fails(agent, tmp_path):
    with pytest.raises(agent.AgentError, match="no current release"):
        agent.boot(tmp_path / "releases", FakeRun())


def flaky(fake, failures):
    """Fails the first `failures` Secrets Manager reads, as right after boot."""
    left = [failures]

    def run(args, env=None, input=None):
        if args[0] == "aws" and left[0] > 0:
            left[0] -= 1
            return subprocess.CompletedProcess(args, 255, "", "Unable to locate credentials")
        return fake(args, env, input)

    return run


def booted_release(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    ship(agent, stage(releases, out, "platform"), FakeRun(store=seeded(out)), tmp_path)
    return out, releases


def test_boot_retries_secret_reads_until_they_answer(agent, tmp_path):
    out, releases = booted_release(agent, tmp_path)
    fake = FakeRun(store=seeded(out))
    slept = []
    agent.boot(releases, flaky(fake, 2), sleep=slept.append)
    assert slept == [20, 20]
    assert len(compose_calls(fake)) == 1


def test_boot_gives_up_after_three_attempts(agent, tmp_path):
    out, releases = booted_release(agent, tmp_path)
    fake = FakeRun(store=seeded(out))
    slept = []
    with pytest.raises(agent.AgentError, match="read secrets"):
        agent.boot(releases, flaky(fake, 3), sleep=slept.append)
    assert slept == [20, 20]
    assert compose_calls(fake) == []


def buildx_asset(version):
    return f"buildx-v{version}.linux-amd64"


def buildx_url(version, name):
    return f"https://github.com/docker/buildx/releases/download/v{version}/{name}"


def test_matching_buildx_plugin_is_left_alone(agent, tmp_path):
    host = agent.load_host(stage(tmp_path / "releases", outputs("standard"), "platform"))
    run = FakeRun()
    agent.ensure_buildx_plugin(host, run)
    assert run.argv("docker", "buildx", "version") and not run.argv("curl")


def test_buildx_plugin_is_replaced_after_its_checksum_verifies(agent, tmp_path):
    host = agent.load_host(stage(tmp_path / "releases", outputs("standard"), "platform"))
    version = host["buildx_plugin_version"]
    run = FakeRun(buildx_version="0.17.0")
    binary = b"buildx-binary"
    asset = buildx_asset(version)
    run.downloads[buildx_url(version, asset)] = binary
    run.downloads[buildx_url(version, "checksums.txt")] = (
        f"{'1' * 64} *buildx-v{version}.darwin-arm64\n"
        f"{hashlib.sha256(binary).hexdigest()} *{asset}\n"
    ).encode()
    agent.ensure_buildx_plugin(host, run)
    assert {a[-1] for a in run.argv("curl")} == {
        buildx_url(version, asset),
        buildx_url(version, "checksums.txt"),
    }
    (install,) = run.argv("install")
    assert install[-1] == f"{host['plugin_dir']}/docker-buildx" and "0755" in install


def test_buildx_plugin_with_a_bad_checksum_is_refused(agent, tmp_path):
    host = agent.load_host(stage(tmp_path / "releases", outputs("standard"), "platform"))
    version = host["buildx_plugin_version"]
    run = FakeRun(buildx_version="0.17.0")
    asset = buildx_asset(version)
    run.downloads[buildx_url(version, asset)] = b"tampered"
    run.downloads[buildx_url(version, "checksums.txt")] = f"{'0' * 64} *{asset}\n".encode()
    with pytest.raises(agent.AgentError, match="checksum"):
        agent.ensure_buildx_plugin(host, run)
    assert not run.argv("install")


def test_ship_ensures_both_cli_plugins(agent, tmp_path):
    out = outputs("standard")
    run = FakeRun(store=seeded(out))
    ship(agent, stage(tmp_path / "releases", out, "platform"), run, tmp_path)
    assert run.argv("docker", "compose", "version") and run.argv("docker", "buildx", "version")


def test_matching_compose_plugin_is_left_alone(agent, tmp_path):
    host = agent.load_host(stage(tmp_path / "releases", outputs("standard"), "platform"))
    run = FakeRun(compose_version=f"v{VERSIONS.docker_compose_plugin}")
    agent.ensure_compose_plugin(host, run)
    assert not run.argv("curl") and not run.argv("install")


def plugin_url(version):
    return (
        f"https://github.com/docker/compose/releases/download/v{version}/"
        "docker-compose-linux-x86_64"
    )


def test_compose_plugin_is_replaced_after_its_checksum_verifies(agent, tmp_path):
    host = agent.load_host(stage(tmp_path / "releases", outputs("standard"), "platform"))
    run = FakeRun(compose_version="2.20.0")
    url = plugin_url(host["compose_plugin_version"])
    binary = b"compose-binary"
    run.downloads[url] = binary
    run.downloads[url + ".sha256"] = (
        f"{hashlib.sha256(binary).hexdigest()} *docker-compose-linux-x86_64\n".encode()
    )
    agent.ensure_compose_plugin(host, run)
    assert {a[-1] for a in run.argv("curl")} == {url, url + ".sha256"}
    (install,) = run.argv("install")
    assert install[-1] == f"{host['plugin_dir']}/docker-compose" and "0755" in install


def test_compose_plugin_with_a_bad_checksum_is_refused(agent, tmp_path):
    host = agent.load_host(stage(tmp_path / "releases", outputs("standard"), "platform"))
    run = FakeRun(compose_version="2.20.0")
    url = plugin_url(host["compose_plugin_version"])
    run.downloads[url] = b"tampered"
    run.downloads[url + ".sha256"] = b"0" * 64 + b" *docker-compose-linux-x86_64\n"
    with pytest.raises(agent.AgentError, match="checksum"):
        agent.ensure_compose_plugin(host, run)
    assert not run.argv("install")


def test_main_reports_the_failed_step_on_one_line(agent, tmp_path, capsys, monkeypatch):
    releases = tmp_path / "releases"
    monkeypatch.setattr(agent, "RELEASES", releases)
    code = agent.main(["rollback"], run=FakeRun())
    err = capsys.readouterr().err.strip().splitlines()
    assert code != 0 and len(err) == 1 and "rollback" in err[0]


@pytest.mark.parametrize(
    ("failing", "step"),
    [
        (["docker", "compose", "version"], "compose plugin"),
        (["docker", "buildx", "version"], "buildx plugin"),
        (["aws"], "generated secrets"),
        (["docker", "run"], "setup SQL"),
        (["docker", "compose", "--project-name"], "compose up"),
    ],
)
def test_main_names_the_failed_step(agent, tmp_path, capsys, monkeypatch, failing, step):
    out = outputs("mcp_only")
    release = stage(tmp_path / "releases", out, "mcp")
    with_certs(agent, [f"mcp.{DOMAIN}"])
    monkeypatch.setattr(agent, "RELEASES", tmp_path / "releases")
    fake = FakeRun(store=master(out, "state"))

    def run(args, env=None, input=None):
        if args[: len(failing)] == failing:
            return subprocess.CompletedProcess(args, 7, "", "boom")
        return fake(args, env, input)

    code = agent.main(["ship", "--release", str(release)], run=run)
    err = capsys.readouterr().err.strip().splitlines()
    (line,) = [x for x in err if not x.startswith(agent.LOG_PREFIX)]
    assert code != 0 and f"ship: {step}:" in line, line


def test_failing_to_stop_nginx_fails_before_certbot(agent, tmp_path):
    host = agent.load_host(stage(tmp_path / "releases", outputs("standard"), "platform"))
    fake = FakeRun(nginx_ids="abc\n")

    def run(args, env=None, input=None):
        if args[:2] == ["docker", "stop"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        return fake(args, env, input)

    with pytest.raises(agent.AgentError, match="NGINX"):
        agent.ensure_certificates(host, outputs("standard"), run, lambda s: None, resolves_here)
    assert not fake.argv("docker", "run")


def test_main_hides_unexpected_exception_messages(agent, tmp_path, capsys, monkeypatch):
    def boom(args, env=None, input=None):
        raise RuntimeError("value-of-secret")

    out = outputs("mcp_only")
    release = stage(tmp_path / "releases", out, "mcp")
    monkeypatch.setattr(agent, "RELEASES", tmp_path / "releases")
    code = agent.main(["ship", "--release", str(release)], run=boom)
    err = capsys.readouterr().err
    assert code != 0 and "value-of-secret" not in err and "RuntimeError" in err


def test_cli_runs_under_the_system_python(tmp_path):
    r = subprocess.run(
        [sys.executable, str(APPLY), "--help"], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0 and "ship" in r.stdout and "boot" in r.stdout


def test_project_override_is_a_second_compose_file(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform")
    (release / "orchestration").mkdir()
    (release / "orchestration" / "docker-compose.override.yml").write_text("services: {}\n")
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    (up,) = compose_calls(run)
    args = up["args"]
    files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
    stack = str(release.resolve() / ".vecten" / "stack")
    override = str(release.resolve() / "orchestration" / "docker-compose.override.yml")
    assert files == [f"{stack}/docker-compose.yml", override]


def test_without_an_override_compose_gets_one_file(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform")
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    (up,) = compose_calls(run)
    assert up["args"].count("-f") == 1


def test_reshipping_the_current_release_recreates_its_containers(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform")
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    ship(agent, release, run, tmp_path)
    first, second = compose_calls(run)
    assert "--force-recreate" not in first["args"]
    assert "--force-recreate" in second["args"]


def test_shipping_a_new_release_does_not_force_recreate(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    run = FakeRun(store=seeded(out))
    ship(agent, stage(releases, out, "platform", commit="c1"), run, tmp_path)
    ship(agent, stage(releases, out, "platform", commit="c2"), run, tmp_path)
    assert all("--force-recreate" not in c["args"] for c in compose_calls(run))


class FailingFirstUp:
    """Fails the compose up of the release under `failing`, then answers like `fake`."""

    def __init__(self, fake, failing):
        self.fake = fake
        self.failing = str(failing.resolve() / ".vecten" / "stack")

    def __call__(self, args, env=None, input=None):
        if args[:2] == ["docker", "compose"] and "up" in args and self.failing in args:
            self.fake.calls.append({"args": list(args), "env": dict(env or {}), "input": input})
            return subprocess.CompletedProcess(args, 1, "", "")
        return self.fake(args, env, input)


def nginx_starts(run):
    return run.argv("docker", "start")


def test_compose_up_failing_after_the_swap_rolls_back_to_the_previous_release(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = stage(releases, out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out), nginx_ids="ngx1\n")
    ship(agent, first, fake, tmp_path)
    second = stage(releases, out, "platform", commit="c2")
    fake.calls.clear()
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, second, FailingFirstUp(fake, second), tmp_path)
    message = str(exc.value)
    assert message.startswith("compose up: docker compose up failed (exit 1)")
    assert message.endswith("; rolled back to c1")
    assert os.readlink(releases / "current") == str(first.resolve())
    failed, restored = compose_calls(fake)
    assert str(second.resolve() / ".vecten" / "stack") in failed["args"]
    assert str(first.resolve() / ".vecten" / "stack") in restored["args"]
    assert "--force-recreate" in restored["args"]
    assert restored["env"]["VECTEN_BUNDLE_COMMIT"] == "c1"
    assert (agent.RUN_DIR / "htpasswd").is_file()
    assert nginx_starts(fake) == [["docker", "start", "ngx1"]]


LEGACY_DROPPED = ("cloud", "project_id", "bucket", "plugin_dir")


def legacy(release):
    """A release built before host.json carried the cloud keys."""
    path = release / ".vecten" / "ship" / "host.json"
    doc = json.loads(path.read_text())
    for key in LEGACY_DROPPED:
        del doc[key]
    path.write_text(json.dumps(doc))
    return release


def test_load_host_gives_an_older_release_its_aws_values(agent, tmp_path):
    out = outputs("standard")
    host = agent.load_host(legacy(stage(tmp_path / "releases", out, "platform")))
    assert host["cloud"] == "aws" and host["project_id"] == ""
    assert host["plugin_dir"] == "/usr/libexec/docker/cli-plugins"
    assert host["bucket"] == out["bucket"] and host["secret_prefix"] == out["secret_prefix"]


def test_load_host_gives_a_release_from_before_services_no_service(agent, tmp_path):
    host = agent.load_host(stage(tmp_path / "releases", outputs("standard"), "platform"))
    assert host["service_secrets"] == [] and host["service_network"] == ""


def test_rollback_onto_a_release_from_before_the_cloud_keys(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = legacy(stage(releases, out, "platform", commit="c1"))
    run = FakeRun(store=seeded(out))
    ship(agent, first, run, tmp_path)
    ship(agent, stage(releases, out, "platform", commit="c2"), run, tmp_path)
    run.calls.clear()
    agent.rollback(releases, run)
    assert os.readlink(releases / "current") == str(first.resolve())
    (up,) = compose_calls(run)
    assert "--force-recreate" in up["args"]
    assert up["env"]["VECTEN_PROVIDER"] == "aws" and up["env"]["VECTEN_BUNDLE_COMMIT"] == "c1"
    assert up["env"]["VECTEN_ARTIFACT_URL"] == f"s3://{out['bucket']}/platform/"
    assert run.argv("aws") and not [k for k in up["env"] if k.startswith("SCW_")]


def test_recovery_onto_a_release_from_before_the_cloud_keys(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = legacy(stage(releases, out, "platform", commit="c1"))
    fake = FakeRun(store=seeded(out))
    ship(agent, first, fake, tmp_path)
    second = stage(releases, out, "platform", commit="c2")
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, second, FailingFirstUp(fake, second), tmp_path)
    assert str(exc.value).endswith("; rolled back to c1")
    assert os.readlink(releases / "current") == str(first.resolve())


def test_first_ship_failing_after_the_swap_has_nothing_to_roll_back_to(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out))
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingFirstUp(fake, release), tmp_path)
    assert str(exc.value).endswith("; no previous release to roll back to")
    assert len(compose_calls(fake)) == 1


def test_a_failed_rollback_after_the_swap_is_named(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = stage(releases, out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out))
    ship(agent, first, fake, tmp_path)
    second = stage(releases, out, "platform", commit="c2")

    def run(args, env=None, input=None):
        if args[:2] == ["docker", "compose"] and "up" in args:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return fake(args, env, input)

    with pytest.raises(agent.AgentError) as exc:
        ship(agent, second, run, tmp_path)
    assert str(exc.value).endswith(
        "; rolling back to c1 failed: compose up: docker compose up failed (exit 1)"
    )


def test_swap_failing_before_current_moves_leaves_the_running_release(agent, tmp_path, monkeypatch):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = stage(releases, out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out))
    ship(agent, first, fake, tmp_path)
    second = stage(releases, out, "platform", commit="c2")

    def broken_swap(releases, new):
        raise OSError("disk full")

    monkeypatch.setattr(agent, "swap", broken_swap)
    fake.calls.clear()
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, second, fake, tmp_path)
    assert str(exc.value) == "swap: failed with OSError; rolled back to c1"
    assert os.readlink(releases / "current") == str(first.resolve())
    assert compose_calls(fake) == []


def test_swap_failing_after_current_moved_rolls_back(agent, tmp_path, monkeypatch):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = stage(releases, out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out))
    ship(agent, first, fake, tmp_path)
    second = stage(releases, out, "platform", commit="c2")
    real_swap = agent.swap

    def half_swap(releases, new):
        real_swap(releases, new)
        raise OSError("pruning failed")

    monkeypatch.setattr(agent, "swap", half_swap)
    fake.calls.clear()
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, second, fake, tmp_path)
    assert str(exc.value) == "swap: failed with OSError; rolled back to c1"
    assert os.readlink(releases / "current") == str(first.resolve())
    (restored,) = compose_calls(fake)
    assert str(first.resolve() / ".vecten" / "stack") in restored["args"]


def test_run_files_failing_after_certbot_restarts_nginx(agent, tmp_path, monkeypatch):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out), nginx_ids="ngx1\n")

    def broken(host, env, run_dir):
        raise OSError("tmpfs full")

    monkeypatch.setattr(agent, "write_run_files", broken)
    with pytest.raises(agent.AgentError, match="run files"):
        agent.ship(
            release, fake, releases=release.parent, sleep=lambda s: None, resolve=resolves_here
        )
    calls = [c["args"][:2] for c in fake.calls]
    assert calls.index(["docker", "stop"]) < calls.index(["docker", "start"])
    assert nginx_starts(fake)[-1] == ["docker", "start", "ngx1"]
    assert compose_calls(fake) == []
    assert not (release.parent / "current").exists()


def test_main_reports_the_rollback_on_its_one_line(agent, tmp_path, capsys, monkeypatch):
    out = outputs("standard")
    releases = tmp_path / "releases"
    monkeypatch.setattr(agent, "RELEASES", releases)
    first = stage(releases, out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out))
    ship(agent, first, fake, tmp_path)
    second = stage(releases, out, "platform", commit="c2")
    code = agent.main(["ship", "--release", str(second)], run=FailingFirstUp(fake, second))
    (line,) = capsys.readouterr().err.strip().splitlines()
    assert code == 1
    assert line == (
        "vecten agent ship: compose up: docker compose up failed (exit 1); rolled back to c1"
    )


CRASHING = [
    ("a1", "mcp-server", "restarting", "Restarting (1) 3 seconds ago"),
    ("b2", "nginx", "created", "Created"),
    ("c3", "airflow-init", "exited", "Exited (0) 2 minutes ago"),
    ("d4", "airflow-scheduler", "running", "Up 2 minutes (healthy)"),
]
CRASH_LOG = (
    "",
    "Traceback (most recent call last):\n"
    '  File "/app/server.py", line 9, in <module>\n'
    "ValueError: provisioner has unexpected grant OWNERSHIP on ADA_LOVELACE\n\n",
)


def crashing_fake(out, **kw):
    return FakeRun(store=seeded(out), containers=CRASHING, logs={"a1": CRASH_LOG}, **kw)


def test_failed_compose_up_carries_the_failing_containers_log(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = stage(releases, out, "platform", commit="c1")
    fake = crashing_fake(out)
    ship(agent, first, fake, tmp_path)
    second = stage(releases, out, "platform", commit="c2")
    fake.calls.clear()
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, second, FailingFirstUp(fake, second), tmp_path)
    assert exc.value.log == (
        "mcp-server: Traceback (most recent call last):",
        'mcp-server: File "/app/server.py", line 9, in <module>',
        "mcp-server: ValueError: provisioner has unexpected grant OWNERSHIP on ADA_LOVELACE",
    )
    assert str(exc.value).endswith("; rolled back to c1")
    calls = [c["args"] for c in fake.calls]
    logs_at = calls.index(["docker", "logs", "--tail", "20", "a1"])
    failed, restored = [
        i for i, a in enumerate(calls) if a[:2] == ["docker", "compose"] and "up" in a
    ]
    # The rollback recreates the containers, and their logs with them.
    assert failed < logs_at < restored


def test_the_excerpt_masks_injected_secret_values(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    secret = "value-of-LOADER_PASSWORD"
    logs = {"a1": (f"connecting as loader:{secret}@db\n", "\x1b[31mfatal\x1b[0m\n")}
    fake = FakeRun(store=seeded(out), containers=CRASHING[:1], logs=logs)
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingFirstUp(fake, release), tmp_path)
    assert exc.value.log == ("mcp-server: connecting as loader:***@db", "mcp-server: fatal")


def test_a_healthy_or_created_stack_adds_no_log_lines(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out), containers=CRASHING[1:], logs={"c3": ("done\n", "")})
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingFirstUp(fake, release), tmp_path)
    assert exc.value.log == ()
    assert fake.argv("docker", "logs") == []


def test_unhealthy_dead_and_failed_containers_are_read(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    rows = [
        ("e5", "mcp-server", "running", "Up 1 minute (unhealthy)"),
        ("f6", "airflow-api-server", "exited", "Exited (137) 5 seconds ago"),
        ("g7", "airflow-worker", "dead", "Dead"),
    ]
    logs = {name: (f"{name} said\n", "") for name, *_ in rows}
    fake = FakeRun(store=seeded(out), containers=rows, logs=logs)
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingFirstUp(fake, release), tmp_path)
    assert exc.value.log == (
        "mcp-server: e5 said",
        "airflow-api-server: f6 said",
        "airflow-worker: g7 said",
    )


def test_the_excerpt_is_capped(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    rows = [(f"id{i}", f"svc{i}", "restarting", "Restarting (1)") for i in range(3)]
    noisy = "".join(f"line {n} " + "x" * 400 + "\n" for n in range(20))
    fake = FakeRun(store=seeded(out), containers=rows, logs={r[0]: (noisy, "") for r in rows})
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingFirstUp(fake, release), tmp_path)
    assert len(exc.value.log) == agent.LOG_EXCERPT_MAX
    assert all(len(line) <= agent.SHIP_LINE_MAX for line in exc.value.log)


def test_docker_failing_during_the_excerpt_keeps_the_ship_error(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    fake = crashing_fake(out)
    fake.ps_fails = True
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingFirstUp(fake, release), tmp_path)
    assert exc.value.log == ()
    assert str(exc.value) == (
        "compose up: docker compose up failed (exit 1); no previous release to roll back to"
    )


def test_main_prints_log_lines_before_its_one_line(agent, tmp_path, capsys, monkeypatch):
    out = outputs("standard")
    releases = tmp_path / "releases"
    monkeypatch.setattr(agent, "RELEASES", releases)
    release = stage(releases, out, "platform", commit="c1")
    with_certs(agent, json.loads((release / ".vecten/ship/host.json").read_text())["hostnames"])
    monkeypatch.setattr(agent, "resolve", resolves_here)
    fake = crashing_fake(out)
    code = agent.main(["ship", "--release", str(release)], run=FailingFirstUp(fake, release))
    lines = capsys.readouterr().err.strip().splitlines()
    assert code == 1
    assert lines[0] == "vecten agent log: mcp-server: Traceback (most recent call last):"
    assert lines[2] == (
        "vecten agent log: mcp-server: ValueError: provisioner has unexpected grant OWNERSHIP "
        "on ADA_LOVELACE"
    )
    assert lines[3].startswith("vecten agent ship: compose up: docker compose up failed")


def test_rollback_prints_the_failing_containers_first(agent, tmp_path, capsys, monkeypatch):
    out = outputs("standard")
    releases = tmp_path / "releases"
    monkeypatch.setattr(agent, "RELEASES", releases)
    fake = FakeRun(store=seeded(out))
    ship(agent, stage(releases, out, "platform", commit="c1"), fake, tmp_path)
    ship(agent, stage(releases, out, "platform", commit="c2"), fake, tmp_path)
    fake.containers, fake.logs = CRASHING, {"a1": CRASH_LOG}
    fake.calls.clear()
    assert agent.main(["rollback"], run=fake) == 0
    lines = capsys.readouterr().err.strip().splitlines()
    assert lines[-1] == (
        "vecten agent log: mcp-server: ValueError: provisioner has unexpected grant OWNERSHIP "
        "on ADA_LOVELACE"
    )
    calls = [c["args"] for c in fake.calls]
    up = next(i for i, a in enumerate(calls) if a[:2] == ["docker", "compose"] and "up" in a)
    assert calls.index(["docker", "logs", "--tail", "20", "a1"]) < up


class FailingUpWith:
    """Fails every compose up with `stderr`, then answers like `fake`."""

    def __init__(self, fake, stderr):
        self.fake, self.stderr = fake, stderr

    def __call__(self, args, env=None, input=None):
        if args[:2] == ["docker", "compose"] and "up" in args:
            self.fake.calls.append({"args": list(args), "env": dict(env or {}), "input": input})
            return subprocess.CompletedProcess(args, 1, "", self.stderr)
        return self.fake(args, env, input)


def test_a_failed_compose_up_reports_compose_output(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    stderr = (
        " Service airflow-scheduler  Building\n\n"
        "#5 ERROR: pull access denied for nginx\n"
        "Error response from daemon: toomanyrequests\n"
    )
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingUpWith(FakeRun(store=seeded(out)), stderr), tmp_path)
    assert exc.value.log == (
        "compose: Service airflow-scheduler  Building",
        "compose: #5 ERROR: pull access denied for nginx",
        "compose: Error response from daemon: toomanyrequests",
    )
    assert str(exc.value) == (
        "compose up: docker compose up failed (exit 1); no previous release to roll back to"
    )


def test_compose_output_is_masked_and_only_its_tail_is_kept(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    noise = "".join(f"#{n} building layer {n}\n" for n in range(100))
    stderr = noise + "invalid interpolation: value-of-LOADER_PASSWORD\n"
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingUpWith(FakeRun(store=seeded(out)), stderr), tmp_path)
    assert len(exc.value.log) == agent.COMPOSE_TAIL_LINES
    assert exc.value.log[-1] == "compose: invalid interpolation: ***"
    assert exc.value.log[0] == "compose: #81 building layer 81"


def test_compose_output_comes_before_the_failing_containers(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", commit="c1")
    fake = crashing_fake(out)
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingUpWith(fake, "dependency failed to start\n"), tmp_path)
    assert exc.value.log[0] == "compose: dependency failed to start"
    assert exc.value.log[1] == "mcp-server: Traceback (most recent call last):"


def test_step_keeps_the_log_of_the_error_it_names(agent):
    with pytest.raises(agent.AgentError) as exc:
        with agent.step("compose up"):
            raise agent.AgentError("failed", ("compose: boom",))
    assert str(exc.value) == "compose up: failed" and exc.value.log == ("compose: boom",)


def _never_started(release):
    return (release.resolve() / ".vecten" / "never-started").exists()


def failed_first_ship(agent, tmp_path, out):
    releases = tmp_path / "releases"
    first = stage(releases, out, "platform", commit="c1")
    fake = FakeRun(store=seeded(out))
    with pytest.raises(agent.AgentError):
        ship(agent, first, FailingFirstUp(fake, first), tmp_path)
    return releases, first, fake


def test_a_failed_first_ship_marks_its_release_never_started(agent, tmp_path):
    _, first, _ = failed_first_ship(agent, tmp_path, outputs("standard"))
    assert _never_started(first)


def test_a_never_started_release_is_never_a_rollback_target(agent, tmp_path):
    out = outputs("standard")
    releases, first, fake = failed_first_ship(agent, tmp_path, out)
    second = stage(releases, out, "platform", commit="c2")
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, second, FailingFirstUp(fake, second), tmp_path)
    assert str(exc.value).endswith("; no previous release to roll back to")
    assert not (releases / "previous").exists()
    assert not first.exists()


def test_a_ship_after_a_never_started_release_keeps_no_previous(agent, tmp_path):
    out = outputs("standard")
    releases, _, fake = failed_first_ship(agent, tmp_path, out)
    ship(agent, stage(releases, out, "platform", commit="c2"), fake, tmp_path)
    assert not (releases / "previous").exists()


def test_a_reship_that_starts_the_release_clears_its_mark(agent, tmp_path):
    out = outputs("standard")
    releases, first, fake = failed_first_ship(agent, tmp_path, out)
    ship(agent, first, fake, tmp_path)
    assert not _never_started(first)
    ship(agent, stage(releases, out, "platform", commit="c2"), fake, tmp_path)
    assert os.readlink(releases / "previous") == str(first.resolve())


# Against Postgres 16: the setup SQL stores the agent's verifiers, people log in with plaintext


@pytest.mark.docker
@pytest.mark.skipif(not docker_util.docker_available(), reason="no docker daemon")
def test_setup_with_agent_verifiers_admits_the_plaintext_passwords(agent, tmp_path):
    import psycopg

    init = tmp_path / "init"
    (init / "vecten").mkdir(parents=True)
    shutil.copy(setup_sql_path(), init / "vecten" / "warehouse_setup.sql")
    passwords = {
        "AIRFLOW_DB_PASSWORD": "a1-plain",
        "MCP_STATE_DB_PASSWORD": "m1-plain",
        "LOADER_PASSWORD": "l1-plain",
        "PROVISIONER_PASSWORD": "p1-plain",
        "IMPERSONATOR_PASSWORD": "i1-plain",
    }
    with docker_util.pg_container(init, {"POSTGRES_PASSWORD": "super"}) as (connect, name):
        with connect("postgres", "super", "postgres") as c:
            c.execute("create role vecten_master login createrole createdb password 'm'")
            c.execute("alter database postgres owner to vecten_master")
            c.execute("alter database template1 owner to vecten_master")
            c.commit()
        cmd = ["docker", "exec", "-e", "PGPASSWORD=m"]
        for key, value in passwords.items():
            cmd += ["-e", f"{key}={agent.scram_verifier(value)}"]
        cmd += [name, "psql", "-h", "127.0.0.1", "-U", "vecten_master", "-d", "postgres"]
        cmd += ["-v", "ON_ERROR_STOP=1"]
        for flag in ("with_airflow", "with_mcp_state", "with_warehouse", "with_mcp"):
            cmd += ["-v", f"{flag}=true"]
        cmd += ["-f", "/docker-entrypoint-initdb.d/vecten/warehouse_setup.sql"]
        setup = subprocess.run(cmd, capture_output=True, text=True)
        assert setup.returncode == 0, setup.stderr
        for role, key, database in (
            ("airflow", "AIRFLOW_DB_PASSWORD", "airflow"),
            ("mcp_state", "MCP_STATE_DB_PASSWORD", "mcp_state"),
            ("loader", "LOADER_PASSWORD", "warehouse"),
            ("provisioner", "PROVISIONER_PASSWORD", "warehouse"),
            ("impersonator", "IMPERSONATOR_PASSWORD", "warehouse"),
        ):
            with connect(role, passwords[key], database) as c:
                assert c.execute("select current_user").fetchone() == (role,)
            with pytest.raises(psycopg.OperationalError):
                connect(role, agent.scram_verifier(passwords[key]), database)
        with connect("postgres", "super", "postgres") as c:
            stored = dict(
                c.execute(
                    "select rolname, rolpassword from pg_authid where rolname = any(%s)",
                    (["airflow", "loader"],),
                ).fetchall()
            )
        assert all(v.startswith("SCRAM-SHA-256$4096:") for v in stored.values())
        assert "a1-plain" not in stored.values() and "l1-plain" not in stored.values()


@pytest.mark.docker
@pytest.mark.skipif(not docker_util.docker_available(), reason="no docker daemon")
def test_setup_runs_again_after_an_admin_login_on_scaleway(agent, tmp_path):
    """Every ship reruns the setup SQL; a Scaleway admin inherits CREATE in the data schemas
    through `loader` and `warehouse_owner`, so the helper install must still succeed."""
    from vecten.deploy.admin import ensure_admin
    from vecten.deploy.scaleway import ADMIN_GRANTS

    init = tmp_path / "init"
    (init / "vecten").mkdir(parents=True)
    shutil.copy(setup_sql_path(), init / "vecten" / "warehouse_setup.sql")
    passwords = {
        "AIRFLOW_DB_PASSWORD": "a1-plain",
        "MCP_STATE_DB_PASSWORD": "m1-plain",
        "LOADER_PASSWORD": "l1-plain",
        "PROVISIONER_PASSWORD": "p1-plain",
        "IMPERSONATOR_PASSWORD": "i1-plain",
    }
    with docker_util.pg_container(init, {"POSTGRES_PASSWORD": "super"}) as (connect, name):
        with connect("postgres", "super", "postgres") as c:
            c.execute("create role vecten_master login createrole createdb password 'm'")
            c.execute("alter database postgres owner to vecten_master")
            c.execute("alter database template1 owner to vecten_master")
            c.commit()
        cmd = ["docker", "exec", "-e", "PGPASSWORD=m"]
        for key, value in passwords.items():
            cmd += ["-e", f"{key}={agent.scram_verifier(value)}"]
        cmd += [name, "psql", "-h", "127.0.0.1", "-U", "vecten_master", "-d", "postgres"]
        cmd += ["-v", "ON_ERROR_STOP=1"]
        for flag in ("with_airflow", "with_mcp_state", "with_warehouse", "with_mcp"):
            cmd += ["-v", f"{flag}=true"]
        cmd += ["-f", "/docker-entrypoint-initdb.d/vecten/warehouse_setup.sql"]
        first = subprocess.run(cmd, capture_output=True, text=True)
        assert first.returncode == 0, first.stderr
        with connect("vecten_master", "m", "warehouse") as c:
            ensure_admin(
                c, "admin_jane", lambda password: None, password_stored=False, grants=ADMIN_GRANTS
            )
        again = subprocess.run(cmd, capture_output=True, text=True)
        assert again.returncode == 0, again.stderr
        with connect("postgres", "super", "warehouse") as c:
            membership = c.execute(
                "select m.set_option, m.inherit_option from pg_auth_members m"
                " join pg_roles r on r.oid = m.roleid join pg_roles u on u.oid = m.member"
                " where r.rolname = 'admin_jane' and u.rolname = 'vecten_master'"
            ).fetchall()
            helpers = c.execute(
                "select count(*) from vecten_access.helpers h join pg_roles r"
                " on r.oid = h.owner_oid where r.rolname = 'admin_jane'"
            ).fetchone()[0]
        # The master may become the admin to install its helper, and reads nothing through it.
        assert (True, False) in membership
        assert helpers > 0


@pytest.mark.parametrize(
    "component,secret", [("platform", "PACKAGE_INDEX_URL"), ("mcp", "MCP_PACKAGE_INDEX_URL")]
)
def test_private_package_index_read_is_component_scoped(agent, tmp_path, component, secret):
    out = outputs("standard")
    host = agent.load_host(stage(tmp_path / "releases", out, component))
    host["private_package_index"] = True
    store = seeded(out, extra=[secret])
    run = FakeRun(store=store)
    env = agent.secret_env(host, out, run)
    assert env[secret] == f"value-of-{secret}"
    other = "MCP_PACKAGE_INDEX_URL" if component == "platform" else "PACKAGE_INDEX_URL"
    assert other not in env


BRIDGE = "vecten-services"
AWS_DROP = ("DOCKER-USER", "-i", BRIDGE, "-d", "169.254.169.254", "-j", "DROP")


def with_services(secrets=()):
    """The host.json keys of a host that runs services declaring `secrets`."""
    return {"service_network": BRIDGE, "service_secrets": list(secrets)}


def kinds(run):
    """The isolation and compose calls, in order."""
    found = []
    for call in run.calls:
        args = call["args"]
        if args[0] == "iptables":
            found.append(f"iptables {args[3]}")
        elif call in compose_calls(run):
            found.append("compose up")
    return found


def test_ship_drops_the_services_bridge_to_the_metadata_address_before_compose(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", **with_services())
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    assert kinds(run) == ["iptables -C", "iptables -I", "compose up"]
    (insert,) = [c["args"] for c in run.calls if c["args"][:4] == ["iptables", "-w", "30", "-I"]]
    assert tuple(insert[4:]) == AWS_DROP


def test_a_rule_already_in_place_is_not_inserted_twice(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", **with_services())
    run = FakeRun(store=seeded(out))
    run.rules.add(AWS_DROP)
    ship(agent, release, run, tmp_path)
    assert kinds(run) == ["iptables -C", "compose up"]


def test_on_scaleway_the_rule_drops_its_metadata_address(agent):
    run = FakeRun()
    host = {"cloud": "scaleway", "service_network": BRIDGE}
    agent.isolate_services(host, run)
    assert run.rules == {("DOCKER-USER", "-i", BRIDGE, "-d", "169.254.42.42", "-j", "DROP")}


def test_a_host_without_services_runs_no_iptables(agent, tmp_path):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform")
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    assert run.argv("iptables") == []


@pytest.mark.parametrize("bridge", ["-j", "vecten services", "a" * 16, "x/y"])
def test_a_bridge_name_that_is_no_interface_name_is_refused(agent, bridge):
    run = FakeRun()
    with pytest.raises(agent.AgentError, match="interface name"):
        agent.isolate_services({"cloud": "aws", "service_network": bridge}, run)
    assert run.calls == []


def test_a_failed_rule_fails_the_ship_and_leaves_the_running_release(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    first = stage(releases, out, "platform", commit="c1")
    run = FakeRun(store=seeded(out), nginx_ids="ngx1\n")
    ship(agent, first, run, tmp_path)
    second = stage(releases, out, "platform", commit="c2", **with_services())
    run.calls.clear()
    run.iptables_fails = True
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, second, run, tmp_path)
    assert str(exc.value).startswith("service isolation: ")
    assert "(exit 4)" in str(exc.value)
    assert compose_calls(run) == []
    assert os.readlink(releases / "current") == str(first.resolve())
    # The certificate step may have stopped NGINX; the running release must keep serving.
    assert nginx_starts(run) == [["docker", "start", "ngx1"]]


def test_main_names_the_isolation_step(agent, tmp_path, capsys, monkeypatch):
    out = outputs("standard")
    release = stage(tmp_path / "releases", out, "platform", **with_services())
    with_certs(agent, [f"airflow.{DOMAIN}"])
    monkeypatch.setattr(agent, "RELEASES", tmp_path / "releases")
    run = FakeRun(store=seeded(out))
    run.iptables_fails = True
    assert agent.main(["ship", "--release", str(release)], run=run) == 1
    (line,) = capsys.readouterr().err.strip().splitlines()
    assert line.startswith("vecten agent ship: service isolation: ")


def test_boot_restores_the_rule_before_it_starts_the_stack(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    release = stage(releases, out, "platform", **with_services())
    ship(agent, release, FakeRun(store=seeded(out)), tmp_path)
    # A reboot clears every iptables rule.
    run = FakeRun(store=seeded(out))
    agent.boot(releases, run)
    assert kinds(run) == ["iptables -C", "iptables -I", "compose up"]


def test_a_failed_rule_at_boot_stops_the_services_docker_restarted(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    release = stage(releases, out, "platform", **with_services())
    ship(agent, release, FakeRun(store=seeded(out)), tmp_path)
    rows = [
        ("s1", "svc-acme-bot", "running", "Up 3 seconds"),
        ("n1", "nginx", "running", "Up 3 seconds"),
        ("a1", "airflow-scheduler", "running", "Up 3 seconds"),
    ]
    run = FakeRun(store=seeded(out), containers=rows)
    run.iptables_fails = True
    with pytest.raises(agent.AgentError, match="^service isolation: "):
        agent.boot(releases, run)
    assert compose_calls(run) == []
    assert run.argv("docker", "stop") == [["docker", "stop", "s1"]]


def test_boot_isolates_before_it_reads_a_secret(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    ship(
        agent,
        stage(releases, out, "platform", **with_services()),
        FakeRun(store=seeded(out)),
        tmp_path,
    )
    run = FakeRun(store=seeded(out))
    agent.boot(releases, run)
    order = [c["args"][0] for c in run.calls if c["args"][0] in ("iptables", "aws")]
    assert order[0] == "iptables" and "aws" in order


def test_a_failed_secret_read_at_boot_still_isolates_the_services(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    ship(
        agent,
        stage(releases, out, "platform", **with_services()),
        FakeRun(store=seeded(out)),
        tmp_path,
    )
    fake = FakeRun(store=seeded(out))
    with pytest.raises(agent.AgentError, match="read secrets"):
        agent.boot(releases, flaky(fake, 3), sleep=lambda s: None)
    assert fake.rules == {AWS_DROP}


def test_the_agents_service_prefix_is_the_compose_one(agent):
    from vecten_shared.services import compose_service_name

    assert compose_service_name("acme", "bot").startswith(agent.SERVICE_PREFIX)


def test_rollback_isolates_a_previous_release_that_runs_services(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    run = FakeRun(store=seeded(out))
    ship(
        agent,
        stage(releases, out, "platform", "c1", **with_services()),
        run,
        tmp_path,
    )
    ship(agent, stage(releases, out, "platform", commit="c2"), run, tmp_path)
    fresh = FakeRun(store=seeded(out))
    agent.rollback(releases, fresh)
    assert kinds(fresh) == ["iptables -C", "iptables -I", "compose up"]


def test_rollback_to_a_release_from_before_services_installs_no_rule(agent, tmp_path):
    out = outputs("standard")
    releases = tmp_path / "releases"
    run = FakeRun(store=seeded(out))
    ship(agent, legacy(stage(releases, out, "platform", commit="c1")), run, tmp_path)
    ship(
        agent,
        stage(releases, out, "platform", "c2", **with_services()),
        run,
        tmp_path,
    )
    fresh = FakeRun(store=seeded(out))
    agent.rollback(releases, fresh)
    assert fresh.argv("iptables") == [] and len(compose_calls(fresh)) == 1


def test_service_secrets_reach_compose_from_their_scope(agent, tmp_path):
    out = outputs("standard")
    services = with_services(("MCP_ACME_TOKEN",))
    release = stage(tmp_path / "releases", out, "mcp", **services)
    store = seeded(out, extra=("MCP_ACME_TOKEN",))
    run = FakeRun(store=store)
    ship(agent, release, run, tmp_path)
    (up,) = compose_calls(run)
    assert up["env"]["MCP_ACME_TOKEN"] == "value-of-MCP_ACME_TOKEN"
    read = [c["args"] for c in run.calls if "get-secret-value" in c["args"]]
    assert f"{PREFIX}mcp/MCP_ACME_TOKEN" in [a[a.index("--secret-id") + 1] for a in read]
    assert not [c for c in run.calls if "value-of-MCP_ACME_TOKEN" in " ".join(c["args"])]


def test_an_unset_service_secret_stays_unset(agent, tmp_path):
    out = outputs("standard")
    services = with_services(("ACME_BOT_TOKEN",))
    release = stage(tmp_path / "releases", out, "platform", **services)
    run = FakeRun(store=seeded(out))
    ship(agent, release, run, tmp_path)
    (up,) = compose_calls(run)
    assert "ACME_BOT_TOKEN" not in up["env"]


def test_a_service_secret_is_masked_in_compose_output(agent, tmp_path):
    out = outputs("standard")
    services = with_services(("ACME_BOT_TOKEN",))
    release = stage(tmp_path / "releases", out, "platform", "c1", **services)
    stderr = "svc-acme-bot: bad token value-of-ACME_BOT_TOKEN\n"
    fake = FakeRun(store=seeded(out, extra=("ACME_BOT_TOKEN",)))
    with pytest.raises(agent.AgentError) as exc:
        ship(agent, release, FailingUpWith(fake, stderr), tmp_path)
    assert exc.value.log[-1] == "compose: svc-acme-bot: bad token ***"
