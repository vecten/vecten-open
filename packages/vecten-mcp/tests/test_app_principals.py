"""An app principal reads only its app's declared datasets, through every tool it can call."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.app import create_app
from vecten_mcp.apps import AppScopes, BoundApp
from vecten_mcp.audit import AuditService
from vecten_mcp.auth.identity import PasswordBackend
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.extensions import McpExtensions
from vecten_mcp.identity import Identity
from vecten_mcp.model import ModelService
from vecten_mcp.runtime import Runtime
from vecten_mcp.state import StateStore
from vecten_shared.config import VectenConfig


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport = _load("app_transport_fixture", "test_mcp_transport.py")
execution = _load("app_query_fixture", "test_query_execution.py")
Secrets, TestClient = transport.Secrets, transport.TestClient
DATA_TOOLS = {"describe", "search", "sql-exec", "query_metrics"}
APP = "acme-sales.overview"


def expression(text):
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": text}]}


MODEL = {
    "version": "0.1.1",
    "semantic_model": [
        {
            "name": "sales",
            "datasets": [
                {
                    "name": "orders",
                    "source": "marts.orders",
                    "fields": [
                        {"name": "amount", "expression": expression("amount")},
                        {"name": "region", "expression": expression("region"), "dimension": {}},
                    ],
                },
                {
                    "name": "customers",
                    "source": "marts.customers",
                    "fields": [{"name": "name", "expression": expression("name"), "dimension": {}}],
                },
                {
                    "name": "payroll",
                    "source": "marts.payroll",
                    "description": "Salaries per employee",
                    "fields": [{"name": "salary", "expression": expression("salary")}],
                },
            ],
            "metrics": [
                {"name": "revenue", "expression": expression("SUM(orders.amount)")},
                {"name": "salaries", "expression": expression("SUM(payroll.salary)")},
            ],
        }
    ],
}


def crm_search(query: str):
    """An extension's tool, which reads data outside any app's declaration."""
    return {"query": query}


@pytest.fixture
def scoped(tmp_path):
    state = StateStore("sqlite:///" + str(tmp_path / "state.db"))
    oauth = OAuthService(
        state,
        SigningKeys(Secrets(), "https://mcp.example"),
        "https://mcp.example",
        PasswordBackend(state),
    )
    config = VectenConfig.model_validate(
        {"project": {"name": "demo", "scaffold_version": "0.1.0"}, "users": 2}
    )
    extensions = McpExtensions.from_config(config)
    extensions.tools["crm-search"] = {"handler": crm_search, "groups": ["*"]}
    runtime = Runtime(state, oauth, extensions, None, None)
    runtime.audit = AuditService(state, start=False)
    runtime.access = AccessService(state, None)
    osi = tmp_path / "osi"
    osi.mkdir()
    (osi / "datasets.json").write_text(json.dumps(MODEL))
    runtime.model = ModelService(osi, state, kind="postgres", source_exists=lambda table: True)
    runtime.model.reload()
    bound = BoundApp(
        extension="acme-sales",
        name="overview",
        title="Sales overview",
        kind="container",
        datasets=("orders", "customers"),
        groups=("default",),
    )
    runtime.apps = AppScopes(state, {"overview": bound}, kind="postgres")

    def service(name):
        principal = state.create_principal(
            Identity("service", name, name + "@service.invalid", name),
            name,
            kind="service",
            provisioned=True,
        )
        credentials = oauth.service_credentials(principal["id"])
        token = oauth.client_credentials(
            credentials["client_id"], credentials["client_secret"], oauth.resource
        )["access_token"]
        return principal, token

    app_principal, app_token = service("svc_app_overview")
    runtime.apps.mark(app_principal["id"], "acme-sales", "overview")
    person = state.create_principal(
        Identity("password", "ada@acme.com", "ada@acme.com", "Ada"), "ada", provisioned=True
    )
    state.put("clients", "person-client", {"used": True})
    with state.transaction() as conn:
        sid, session = oauth._session(person["id"], "person-client", conn)
        person_token = oauth._tokens(sid, session, conn)["access_token"]
    query, _, _, opened, _ = execution.fixture("postgres")
    query.state = state
    runtime.queries = query
    with TestClient(create_app(runtime=runtime), base_url="https://mcp.example") as http:
        yield SimpleNamespace(
            runtime=runtime,
            http=http,
            app=app_principal,
            app_token=app_token,
            person=person,
            person_token=person_token,
            opened=opened,
            service=service,
        )


def rpc(scoped, token, method, params=None, headers=None):
    sent = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "mcp-method": method,
        "Authorization": "Bearer " + token,
        **(headers or {}),
    }
    if params and "name" in params:
        sent["mcp-name"] = params["name"]
    response = scoped.http.post(
        "/mcp",
        headers=sent,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                **(params or {}),
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def call(scoped, name, arguments=None, *, token=None, headers=None):
    return rpc(
        scoped,
        token or scoped.app_token,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        headers,
    )


def text(result):
    return result["content"][0]["text"]


def content(result):
    assert not result.get("isError"), result
    return json.loads(text(result))


def audit_row(scoped, result):
    scoped.runtime.audit.flush()
    return scoped.runtime.audit.find(result["_meta"]["vecten/receipt"]["id"])


def test_an_app_principal_lists_and_calls_only_the_data_tools(scoped):
    listed = rpc(scoped, scoped.app_token, "tools/list")["tools"]
    assert {tool["name"] for tool in listed} == DATA_TOOLS
    for name, arguments in (
        ("crm-search", {"query": "payroll"}),
        ("verify_receipt", {"receipt_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV"}),
        ("report_feedback", {}),
    ):
        result = call(scoped, name, arguments)
        assert result["isError"] and "not available" in text(result)
    person = rpc(scoped, scoped.person_token, "tools/list")["tools"]
    assert {"crm-search", "verify_receipt"} <= {tool["name"] for tool in person}


def test_sql_on_a_declared_table_runs_as_the_app_principal(scoped):
    sql = "SELECT region, sum(amount) FROM marts.orders GROUP BY 1"
    result = call(scoped, "sql-exec", {"sql": sql})
    assert content(result)["rows"] == [[7], [8]]
    assert scoped.opened[0][0]["id"] == scoped.app["id"]
    assert audit_row(scoped, result)["principal_id"] == scoped.app["id"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM marts.payroll",
        "WITH p AS (SELECT * FROM marts.payroll) SELECT * FROM p",
        "SELECT * FROM marts.orders WHERE id IN (SELECT id FROM marts.payroll)",
        'SELECT * FROM "marts"."payroll"',
        "SELECT * FROM marts.orders -- a note",
        "SELECT query_to_xml('select * from marts.payroll', true, false, '')",
        "SELECT * FROM information_schema.tables",
    ],
)
def test_sql_reaching_an_undeclared_relation_is_refused_before_the_warehouse(scoped, sql):
    result = call(scoped, "sql-exec", {"sql": sql})
    assert result["isError"]
    assert APP in text(result)
    assert not scoped.opened
    row = audit_row(scoped, result)
    assert (row["resolution"], row["refusal_reason"]) == ("refused", "app_scope")


def test_the_refusal_names_the_app_and_the_relation(scoped):
    result = call(scoped, "sql-exec", {"sql": "SELECT * FROM marts.payroll"})
    assert "acme-sales.overview" in text(result) and "marts.payroll" in text(result)


def test_an_undeclared_metric_is_refused_and_a_declared_one_runs(scoped):
    refused = call(scoped, "query_metrics", {"metrics": ["salaries"]})
    assert refused["isError"]
    assert APP in text(refused) and "salaries" in text(refused)
    assert not scoped.opened
    missing = call(scoped, "query_metrics", {"metrics": ["no_such_metric"]})
    assert text(missing).replace("no_such_metric", "salaries") == text(refused)
    compiled = content(call(scoped, "query_metrics", {"metrics": ["revenue"]}))
    assert compiled["resolution"] == "compiled"
    assert len(scoped.opened) == 1


def test_describe_and_search_show_declared_datasets_only(scoped):
    described = content(call(scoped, "describe"))
    assert [d["name"] for d in described["datasets"]] == ["customers", "orders"]
    assert [m["name"] for m in described["metrics"]] == ["revenue"]
    hidden = call(scoped, "describe", {"dataset": "payroll"})
    unknown = call(scoped, "describe", {"dataset": "no_such_dataset"})
    assert hidden["isError"] and text(hidden) == text(unknown)
    assert content(call(scoped, "search", {"query": "salar"}))["results"] == []
    assert content(call(scoped, "search", {"query": "amount"}))["results"]
    person = content(call(scoped, "describe", token=scoped.person_token))
    assert "payroll" in [d["name"] for d in person["datasets"]]


def test_a_marked_principal_whose_app_is_no_longer_bound_reads_nothing(scoped):
    principal, token = scoped.service("svc_app_gone")
    scoped.runtime.apps.mark(principal["id"], "acme-sales", "gone")
    result = call(scoped, "sql-exec", {"sql": "SELECT * FROM marts.orders"}, token=token)
    assert result["isError"] and not scoped.opened
    assert content(call(scoped, "describe", token=token))["datasets"] == []


def test_a_person_is_not_scoped(scoped):
    result = call(
        scoped, "sql-exec", {"sql": "SELECT * FROM marts.payroll"}, token=scoped.person_token
    )
    assert not result.get("isError")
    assert scoped.opened[0][0]["id"] == scoped.person["id"]


def test_the_viewer_is_recorded_for_an_app_principal(scoped):
    result = call(
        scoped,
        "sql-exec",
        {"sql": "SELECT amount FROM marts.orders"},
        headers={"X-Vecten-On-Behalf-Of": "ada@acme.com"},
    )
    row = audit_row(scoped, result)
    assert row["principal_id"] == scoped.app["id"]
    assert row["on_behalf_of"] == "ada@acme.com"


def test_a_percent_encoded_viewer_is_recorded_decoded(scoped):
    result = call(scoped, "describe", headers={"X-Vecten-On-Behalf-Of": "ad%C3%A9@acme.com"})
    assert audit_row(scoped, result)["on_behalf_of"] == "adé@acme.com"


@pytest.mark.parametrize(
    "value",
    ["not an email", "ada@acme.com%0Aforged: 1", "a" * 400 + "@x.com", "%FF@acme.com", "a@b@c"],
)
def test_a_malformed_viewer_is_not_recorded(scoped, value):
    result = call(scoped, "describe", headers={"X-Vecten-On-Behalf-Of": value})
    assert "on_behalf_of" not in audit_row(scoped, result)


def test_the_viewer_header_from_a_person_is_ignored(scoped):
    result = call(
        scoped,
        "sql-exec",
        {"sql": "SELECT amount FROM marts.orders"},
        token=scoped.person_token,
        headers={"X-Vecten-On-Behalf-Of": "ada@acme.com"},
    )
    assert not result.get("isError")
    row = audit_row(scoped, result)
    assert row["principal_id"] == scoped.person["id"]
    assert "on_behalf_of" not in row


def test_the_viewer_never_widens_what_the_app_reads(scoped):
    result = call(
        scoped,
        "sql-exec",
        {"sql": "SELECT * FROM marts.payroll"},
        headers={"X-Vecten-On-Behalf-Of": "admin@acme.com"},
    )
    assert result["isError"] and not scoped.opened


def test_no_per_address_limit_applies_to_an_app_principals_tool_calls(scoped):
    # Every call comes from the test client's one address; the token endpoint's per-address
    # limit is 60 a minute, and tool calls are subject to none.
    for _ in range(80):
        assert not call(scoped, "describe").get("isError")
