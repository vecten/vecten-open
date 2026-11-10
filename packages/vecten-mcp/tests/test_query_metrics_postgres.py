"""query_metrics end to end against a disposable Postgres warehouse."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SHARED_TESTS = Path(__file__).parents[2] / "vecten-shared/tests"
FIXTURE = SHARED_TESTS / "fixtures/compiler"
CASES = json.loads((FIXTURE / "cases.json").read_text())


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


util = load("metrics_pg_util", SHARED_TESTS / "docker_util.py")
transport = load("metrics_pg_transport", Path(__file__).with_name("test_mcp_transport.py"))
# The module's tests share one Postgres container and run in order against it, so a CI shard
# takes the module whole.
pytestmark = [pytest.mark.docker, pytest.mark.xdist_group("mcp-query-metrics-postgres")]

WAREHOUSE = """
CREATE ROLE provisioner LOGIN CREATEROLE PASSWORD 'fixture-provisioner';
CREATE ROLE impersonator LOGIN NOINHERIT PASSWORD 'fixture-impersonator';
CREATE ROLE analyst NOLOGIN;
GRANT analyst TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
CREATE SCHEMA marts;
CREATE TABLE marts.orders (
    order_id int, amount numeric, discount numeric, country_code text, channel text,
    status text, customer_email text, order_date date
);
INSERT INTO marts.orders VALUES
    (1, 100, 10, 'pl', 'Web', 'paid', 'a@x', '2026-01-15'),
    (2, 50, 0, 'pl', 'App', 'shipped', 'b@x', '2026-01-20'),
    (3, 70, 5, 'de', 'Web', 'paid', 'a@x', '2026-02-03'),
    (4, 30, 0, 'de', 'Web', 'cancelled', 'c@x', '2026-02-10'),
    (5, 999, 0, 'pl', 'Web', 'paid', 'a@x', '2025-12-31');
CREATE TABLE marts.customers (email text, segment text, signup_date date);
INSERT INTO marts.customers VALUES
    ('a@x', 'smb', '2025-01-01'), ('b@x', 'enterprise', '2025-02-01');
CREATE TABLE marts.returns (return_id int, segment text, returned_at date);
INSERT INTO marts.returns VALUES (1, 'smb', '2026-01-30');
CREATE TABLE marts.balances (account_id int, region text, balance_month date, balance numeric);
INSERT INTO marts.balances VALUES
    (1, 'eu', '2026-01-01', 100), (1, 'eu', '2026-02-01', 120), (1, 'eu', '2026-03-01', 90),
    (2, 'eu', '2026-03-01', 10), (3, 'test-eu', '2026-03-01', 5000);
CREATE TABLE marts.events (event_id int, created_at timestamp, processed_at timestamp);
INSERT INTO marts.events VALUES (1, '2026-01-01 10:00', '2026-01-01 11:00');
GRANT USAGE ON SCHEMA marts TO analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA marts TO analyst;
"""


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    from vecten_mcp.access import AccessService
    from vecten_mcp.administration import Administration
    from vecten_mcp.app import create_app
    from vecten_mcp.audit import AuditService
    from vecten_mcp.auth.identity import PasswordBackend
    from vecten_mcp.auth.keys import SigningKeys
    from vecten_mcp.auth.service import OAuthService
    from vecten_mcp.extensions import McpExtensions
    from vecten_mcp.model import ModelService
    from vecten_mcp.queries import QueryService
    from vecten_mcp.runtime import Runtime
    from vecten_mcp.state import StateStore
    from vecten_mcp.warehouse.pool import PrincipalPools
    from vecten_mcp.warehouse.postgres import PostgresAdapter
    from vecten_shared.config import VectenConfig

    if not util.docker_available():
        pytest.skip("Docker unavailable")
    root = tmp_path_factory.mktemp("query-metrics")
    init = root / "init"
    init.mkdir(mode=0o755)
    osi = root / "osi"
    shutil.copytree(FIXTURE / "osi", osi)
    with util.pg_container(init, {"POSTGRES_PASSWORD": "fixture-master"}) as (connect, name):

        def conn(user="postgres", password="fixture-master"):
            c = connect(user, password, "postgres")
            c.autocommit = True
            return c

        with conn() as c:
            c.execute("ALTER SYSTEM SET log_statement = 'all'")
            c.execute("SELECT pg_reload_conf()")
            c.execute(WAREHOUSE)
        state = StateStore("sqlite:///" + str(root / "state.db"))
        oauth = OAuthService(
            state,
            SigningKeys(transport.Secrets(), "https://mcp.example"),
            "https://mcp.example",
            PasswordBackend(state),
        )
        config = VectenConfig.model_validate(
            {"project": {"name": "demo", "scaffold_version": "0.1.0"}, "users": 2}
        )
        runtime = Runtime(state, oauth, McpExtensions.from_config(config), None, None)
        runtime.audit = AuditService(state, start=False)
        adapter = PostgresAdapter(
            lambda: conn("provisioner", "fixture-provisioner"),
            lambda: conn("impersonator", "fixture-impersonator"),
        )
        runtime.access = AccessService(state, None)
        runtime.administration = Administration(state, adapter, oauth, runtime.access)
        pools = PrincipalPools(adapter)
        runtime.queries = QueryService(state, pools, kind="postgres")
        runtime.model = ModelService(osi, state, kind="postgres", source_exists=lambda table: True)
        credentials = runtime.administration.create_service("metrics")["credentials"]
        token = oauth.client_credentials(
            credentials["client_id"], credentials["client_secret"], oauth.resource
        )["access_token"]
        try:
            with transport.TestClient(
                create_app(runtime=runtime), base_url="https://mcp.example"
            ) as http:
                yield SimpleNamespace(runtime=runtime, http=http, token=token, container=name)
        finally:
            pools.close()


def call(served, name, arguments):
    return transport.rpc(
        served.http, served.token, "tools/call", {"name": name, "arguments": arguments}
    ).json()["result"]


def body(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def statement_logged(container, receipt_id):
    deadline = time.time() + 10
    while time.time() < deadline:
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True).stderr
        if f"/* vecten {receipt_id} */" in logs:
            return True
        time.sleep(0.5)
    return False


def test_compiled_answer_rows_receipt_and_audit_row(served):
    result = call(served, "query_metrics", CASES["compiled"]["full_request"])
    data = body(result)
    assert data["resolution"] == "compiled"
    assert [(row[0], row[1][:10], row[2]) for row in data["rows"]] == [
        ("PL", "2026-01-01", "150"),
        ("DE", "2026-02-01", "70"),
    ]
    receipt = data["receipt"]
    assert receipt == result["_meta"]["vecten/receipt"]
    assert receipt["model"] == served.runtime.model.current.commit
    assert receipt["sql_sha256"] == hashlib.sha256(data["sql"].encode()).hexdigest()
    assert data["definitions"]["metrics"]["total_revenue"]["verified"] is None
    assert data["operations"]["total_revenue"]["rows_may"] == [
        "filter",
        "sort",
        "sum_across_dimensions",
    ]
    served.runtime.audit.flush()
    row = served.runtime.audit.find(receipt["id"])
    assert row["resolution"] == "compiled"
    assert row["metrics"] == ["total_revenue"] and row["dimensions"] == ["country"]
    assert row["filters"] == [{"field": "status", "op": "in", "value": ["paid", "shipped"]}]
    assert row["sql_sha256"] == receipt["sql_sha256"]
    assert statement_logged(served.container, receipt["id"])


@pytest.mark.parametrize("case", sorted(CASES["compiled"]))
def test_every_compiled_case_executes(served, case):
    data = body(call(served, "query_metrics", CASES["compiled"][case]))
    assert data["resolution"] == "compiled"
    assert isinstance(data["rows"], list)


def test_semi_additive_last_and_first_take_the_edge_month(served):
    last = body(call(served, "query_metrics", CASES["compiled"]["semi_additive_last"]))
    assert [(row[0], row[1][:10], row[2]) for row in last["rows"]] == [("eu", "2026-01-01", "100")]
    first = body(call(served, "query_metrics", CASES["compiled"]["semi_additive_first"]))
    assert [(row[0][:10], row[1]) for row in first["rows"]] == [("2026-01-01", "100")]


def test_refusal_skeleton_finished_by_sql_exec_is_chained(served):
    refused = call(served, "query_metrics", CASES["refused"]["join_required_dimension"])
    data = body(refused)
    assert data["reason"] == "join_required"
    assert data["receipt"]["sql_sha256"] is None
    prefix = "SELECT /* complete the requested calculation */ *"
    assert data["skeleton"].startswith(prefix)
    finished = (
        'SELECT "customers".segment, SUM("orders".amount) AS revenue'
        + data["skeleton"][len(prefix) :]
        + ' GROUP BY "customers".segment ORDER BY 1'
    )
    fallback = call(served, "sql-exec", {"sql": finished, "question": "Revenue by segment?"})
    assert body(fallback)["rows"] == [["enterprise", "50"], ["smb", "1169"]]
    chain = refused["_meta"]["vecten/request_chain_id"]
    assert fallback["_meta"]["vecten/request_chain_id"] == chain
    served.runtime.audit.flush()
    rows = [
        served.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])
        for result in (refused, fallback)
    ]
    assert [(row["resolution"], row["request_chain_id"]) for row in rows] == [
        ("refused", chain),
        ("agent_sql", chain),
    ]
    assert rows[0]["refusal_reason"] == "join_required"
