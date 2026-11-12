"""An app principal on the laptop's Postgres: its warehouse roles read more than the app
declares, and the server keeps it to the declared datasets.

One container and one server for the module; the tests run in file order.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("app_served", Path(__file__).with_name("served.py"))
served = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = served
_spec.loader.exec_module(served)

pytestmark = [pytest.mark.docker, pytest.mark.xdist_group("mcp-app-scope-postgres-docker")]

SEED = """
CREATE TABLE marts.orders (order_id int PRIMARY KEY, region text, amount numeric);
INSERT INTO marts.orders VALUES (1, 'eu', 100), (2, 'us', 70);
CREATE TABLE marts.payroll (employee text, salary numeric);
INSERT INTO marts.payroll VALUES ('grace', 9000);
"""
DECLARED = {
    "title": "Sales overview",
    "description": "",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme_sales/app.py",
    "datasets": ["orders"],
    "groups": ["default"],
}


def field(name):
    return {"name": name, "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]}}


MODEL = {
    "version": "0.1.1",
    "semantic_model": [
        {
            "name": "shop",
            "datasets": [
                {"name": "orders", "source": "marts.orders", "fields": [field("amount")]},
                {"name": "payroll", "source": "marts.payroll", "fields": [field("salary")]},
            ],
            "metrics": [
                {
                    "name": "revenue",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.amount)"}]
                    },
                },
                {
                    "name": "salaries",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(payroll.salary)"}]
                    },
                },
            ],
        }
    ],
}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    from vecten_shared.apps import parse_app
    from vecten_shared.config import VectenConfig
    from vecten_shared.extension_record import RECORD_FILE, Extension, record_text
    from vecten_shared.osi.revision import content_revision

    if not served.docker_util.docker_available():
        pytest.skip("Docker unavailable")
    root = tmp_path_factory.mktemp("apps")
    (root / "osi").mkdir()
    (root / "osi" / "shop.json").write_text(json.dumps(MODEL))
    artifacts = root / "artifacts" / "dbt"
    artifacts.mkdir(parents=True)
    (artifacts / "built_commit").write_text(content_revision(root / "osi") + "\n")
    app = parse_app("acme-sales", "overview", DECLARED).app
    (root / RECORD_FILE).write_text(
        record_text({"acme-sales": Extension("1.0", (), apps={"overview": app})})
    )
    config = VectenConfig.model_validate(
        {
            "project": {"name": "shop", "scaffold_version": "0.1.0"},
            "users": 5,
            "components": ["platform", "mcp"],
            "mcp": {"admins": ["ada@example.com"]},
            "extensions": ["acme-sales"],
            "apps": {"acme-sales": {"overview": {"groups": ["default"]}}},
        }
    )
    with (
        served.laptop_postgres(root) as pg,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        pg.sql(SEED, user="loader")
        pg.rerun_setup()
        environ = {"VECTEN_ARTIFACT_DIR": str(root / "artifacts")}
        with served.serve(root, config, pg, monkeypatch, environ=environ) as stack:
            stack.ada = stack.sign_in("ada@example.com")["access_token"]
            created = stack.admin(stack.ada, "services/create", {"name": "app_overview"})["data"]
            creds = created["credentials"]
            stack.principal = created
            stack.token = stack.runtime.oauth.client_credentials(
                creds["client_id"], creds["client_secret"], stack.runtime.oauth.resource
            )["access_token"]
            yield stack


def sql(stack, statement, headers=None):
    response = stack.http.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": served.PROTOCOL,
            "mcp-method": "tools/call",
            "mcp-name": "sql-exec",
            "Authorization": "Bearer " + stack.token,
            **(headers or {}),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "sql-exec",
                "arguments": {"sql": statement},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": served.PROTOCOL,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def rows(result):
    assert not result.get("isError"), result
    return [list(row) for row in json.loads(result["content"][0]["text"])["rows"]]


ATTACKS = [
    "SELECT * FROM marts.payroll",
    "SELECT query_to_xml('select * from marts.payroll', true, false, '')::text",
]


def test_before_it_is_marked_the_principals_roles_read_the_undeclared_table(stack):
    # The control: the default group's schema grant on marts lets this service read payroll,
    # so the refusals below are the server's, not the warehouse's.
    assert rows(sql(stack, ATTACKS[0])) == [["grace", "9000"]]
    assert "9000" in rows(sql(stack, ATTACKS[1]))[0][0]


def test_marked_as_the_apps_principal_it_reads_its_declared_table_as_itself(stack):
    stack.runtime.apps.mark(stack.principal["id"], "acme-sales", "overview")
    assert rows(sql(stack, "SELECT sum(amount) FROM marts.orders")) == [["170"]]
    assert rows(sql(stack, "SELECT current_user FROM marts.orders LIMIT 1")) == [
        [stack.principal["warehouse_name"]]
    ]


@pytest.mark.parametrize("statement", ATTACKS)
def test_the_undeclared_table_is_refused_naming_the_app(stack, statement):
    result = sql(stack, statement)
    assert result["isError"]
    assert "acme-sales.overview" in result["content"][0]["text"]


def test_metrics_and_discovery_hold_the_declared_dataset_only(stack):
    assert stack.call(stack.token, "query_metrics", {"metrics": ["revenue"]})["rows"] == [["170"]]
    refused = stack.call_raw(stack.token, "query_metrics", {"metrics": ["salaries"]})
    assert refused["isError"]
    described = stack.call(stack.token, "describe")
    assert [d["name"] for d in described["datasets"]] == ["orders"]
    assert stack.tools(stack.token) == ["describe", "query_metrics", "search", "sql-exec"]


def test_the_viewer_is_audited_beside_the_app_principal(stack):
    result = sql(
        stack,
        "SELECT region FROM marts.orders",
        headers={"X-Vecten-On-Behalf-Of": "ada@example.com"},
    )
    stack.runtime.audit.flush()
    row = stack.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])
    assert (row["principal_id"], row["on_behalf_of"]) == (stack.principal["id"], "ada@example.com")


def test_the_default_group_is_admitted_through_its_schema_grant(stack):
    assert stack.runtime.apps.admitted("overview") == {"default"}
    assert stack.admin(stack.ada, "access-health")["data"]["apps"] == []


# Row policies on the declared table: `default` (role analyst) sees eu rows only, `finance`
# sees every row but holds no grant on orders, so the app does not admit it.
POLICIES = """
ALTER TABLE marts.orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY eu_only ON marts.orders FOR SELECT TO analyst USING (region = 'eu');
CREATE POLICY every_row ON marts.orders FOR SELECT TO finance USING (true);
"""


def test_a_bound_group_the_app_does_not_admit_never_feeds_its_queries(stack):
    from vecten_mcp.apps import BoundApp

    runtime = stack.runtime
    stack.admin(stack.ada, "groups/create", {"name": "finance"})
    stack.pg.sql(POLICIES, user="loader")
    runtime.apps.apps["overview"] = BoundApp(
        "acme-sales", "overview", "Sales overview", "container", ("orders",), ("default", "finance")
    )
    runtime.access.admit()
    assert runtime.apps.admitted("overview") == {"default"}
    resynced = stack.admin(
        stack.ada, "apps/connect", {"name": "acme-sales.overview", "rotate": False}
    )["data"]
    assert resynced["groups"] == ["default", "finance"]
    assert runtime.state.groups_for(stack.principal["id"]) == {"default", "finance"}
    # Only the eu row: finance's policy would show the us row too.
    assert rows(sql(stack, "SELECT sum(amount) FROM marts.orders")) == [["100"]]


def test_a_role_granted_outside_the_mcp_refuses_the_app_query(stack):
    name = stack.principal["warehouse_name"]
    stack.pg.sql(f"GRANT finance TO {name}")
    try:
        result = sql(stack, "SELECT sum(amount) FROM marts.orders")
        assert result["isError"]
        assert "finance" in result["content"][0]["text"]
    finally:
        stack.pg.sql(f"REVOKE finance FROM {name}")
    stack.pg.sql(f"CREATE ROLE stray_reader NOLOGIN; GRANT stray_reader TO {name}")
    result = sql(stack, "SELECT sum(amount) FROM marts.orders")
    assert result["isError"] and "stray_reader" in result["content"][0]["text"]
