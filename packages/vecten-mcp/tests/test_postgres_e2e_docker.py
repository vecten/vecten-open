"""The MCP served over HTTP on the laptop's Postgres, from provisioning to the feedback loop.

One container and one server for the module; the tests run in file order and build on the
principals, groups and grants the earlier ones created, as an administrator's session would.
"""

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import psycopg
import pytest

# Every package's tests import as `tests`, so a whole-repository run resolves a relative import
# against another package; the harness is loaded by its path instead.
_spec = importlib.util.spec_from_file_location("e2e_served", Path(__file__).with_name("served.py"))
served = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = served
_spec.loader.exec_module(served)
docker_util, laptop_postgres, rpc, serve = (
    served.docker_util,
    served.laptop_postgres,
    served.rpc,
    served.serve,
)

# The module's tests share one Postgres container and run in order against it, so a CI shard
# takes the module whole.
pytestmark = [pytest.mark.docker, pytest.mark.xdist_group("mcp-postgres-e2e-docker")]

PERSON_TOOLS = {"describe", "search", "sql-exec", "query_metrics", "report_feedback"}
PERSON_TOOLS |= {"verify_receipt"}
ADMIN_TOOLS = {
    "list_gaps",
    "read_gap",
    "set_gap_status",
    "list_users",
    "list_groups",
    "usage",
    "model_health",
    "connected_clients",
    "usage_view",
    "search_audit",
    "read_audit_row",
    "list_grants",
    "read_settings",
    "list_apps",
}
SEED = """
CREATE TABLE marts.orders (
    order_id int PRIMARY KEY, region text, amount numeric, customer_email text, order_date date
);
INSERT INTO marts.orders VALUES
    (1, 'eu', 100, 'a@x.test', '2026-01-15'),
    (2, 'eu', 50, 'b@x.test', '2026-02-20'),
    (3, 'us', 70, 'c@x.test', '2026-02-03');
ALTER TABLE marts.orders ENABLE ROW LEVEL SECURITY;
CREATE POLICY eu_only ON marts.orders FOR SELECT USING (region = 'eu');
CREATE SCHEMA raw_shop;
CREATE TABLE raw_shop.events (id int);
INSERT INTO raw_shop.events VALUES (1), (2);
CREATE TABLE raw_shop.carts (id int);
"""


def field(name, *, dimension=None):
    entry = {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]},
    }
    if dimension is not None:
        entry["dimension"] = dimension
    return entry


MODEL = {
    "version": "0.1.1",
    "semantic_model": [
        {
            "name": "shop",
            "datasets": [
                {
                    "name": "orders",
                    "source": "warehouse.marts.orders",
                    "primary_key": ["order_id"],
                    "fields": [
                        field("order_id"),
                        field("amount"),
                        field("region", dimension={}),
                        field("customer_email", dimension={}),
                        field("order_date", dimension={"is_time": True}),
                    ],
                },
                {
                    "name": "events",
                    "source": "warehouse.raw_shop.events",
                    "fields": [field("id")],
                },
            ],
            "metrics": [
                {
                    "name": "total_amount",
                    "description": "Sum of order amounts.",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.amount)"}]
                    },
                }
            ],
        }
    ],
}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    from vecten_shared.config import VectenConfig
    from vecten_shared.osi.revision import content_revision

    if not docker_util.docker_available():
        pytest.skip("Docker unavailable")
    root = tmp_path_factory.mktemp("e2e")
    (root / "osi").mkdir()
    (root / "osi" / "shop.json").write_text(json.dumps(MODEL))
    artifacts = root / "artifacts" / "dbt"
    artifacts.mkdir(parents=True)
    # The platform's dbt build of this model has run: the model may go live at once.
    (artifacts / "built_commit").write_text(content_revision(root / "osi") + "\n")
    config = VectenConfig.model_validate(
        {
            "project": {"name": "shop", "scaffold_version": "0.1.0"},
            "users": 5,
            "components": ["platform", "mcp"],
            "mcp": {"admins": ["ada@example.com"]},
        }
    )
    with laptop_postgres(root) as pg, pytest.MonkeyPatch.context() as monkeypatch:
        pg.sql(SEED, user="loader")
        pg.rerun_setup()
        environ = {"VECTEN_ARTIFACT_DIR": str(root / "artifacts")}
        with serve(root, config, pg, monkeypatch, environ=environ) as served:
            served.ada = served.sign_in("ada@example.com")["access_token"]
            served.people = {}
            yield served


def person(stack, key, email, name):
    """Create, once, a provisioned person and return their access token."""
    if key not in stack.people:
        created = stack.admin(stack.ada, "users/create", {"email": email, "name": name})["data"]
        token = stack.sign_in(email)
        stack.people[key] = {**created, **token}
    return stack.people[key]


def sql(stack, token, statement):
    return stack.call(token, "sql-exec", {"sql": statement})


def rows(answer):
    return [list(row) for row in answer["rows"]]


def members_of(stack, role):
    found = stack.pg.sql(
        "SELECT u.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid "
        "JOIN pg_roles u ON u.oid=m.member WHERE r.rolname=%s",
        db="postgres",
        params=(role,),
    )
    return {row[0] for row in found}


def test_admin_provisions_a_person_and_adopts_an_existing_role(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    role = grace["warehouse_name"]
    login = stack.pg.sql(
        "SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", db="postgres", params=(role,)
    )
    assert login == [(False,)]
    provisioner = stack.pg.sql(
        "SELECT m.admin_option, m.inherit_option, m.set_option FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member "
        "WHERE r.rolname=%s AND u.rolname='provisioner'",
        db="postgres",
        params=(role,),
    )
    assert provisioner == [(True, False, False)]
    stack.pg.sql("CREATE ROLE legacy_ana NOLOGIN", db="postgres")
    oid = stack.pg.sql("SELECT oid FROM pg_roles WHERE rolname='legacy_ana'", db="postgres")
    refused = stack.admin(
        stack.ada,
        "users/create",
        {"email": "ana@example.com", "name": "Ana", "warehouse_name": "legacy_ana"},
        status=409,
    )
    assert refused["data"]["adoption_required"] is True
    stack.pg.sql(refused["data"]["account"]["adoption_sql"], db="postgres")
    adopted = stack.admin(
        stack.ada,
        "users/create",
        {"email": "ana@example.com", "name": "Ana", "warehouse_name": "legacy_ana", "adopt": True},
    )["data"]
    assert adopted["warehouse_name"] == "legacy_ana" and adopted["adopted"] is True
    assert stack.pg.sql("SELECT oid FROM pg_roles WHERE rolname='legacy_ana'", db="postgres") == oid


def test_person_queries_as_their_own_role_through_impersonator(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    answer = sql(stack, grace["access_token"], "SELECT current_user, session_user")
    assert rows(answer) == [[grace["warehouse_name"], "impersonator"]]
    assert "impersonator" in members_of(stack, grace["warehouse_name"])


def test_service_principal_queries_as_its_service_role(stack):
    service = stack.admin(stack.ada, "services/create", {"name": "dashboards"})["data"]
    creds = service["credentials"]
    token = stack.runtime.oauth.client_credentials(
        creds["client_id"], creds["client_secret"], stack.runtime.oauth.resource
    )["access_token"]
    answer = sql(stack, token, "SELECT current_user")
    assert rows(answer) == [[service["warehouse_name"]]]
    assert service["warehouse_name"].startswith("svc_")
    stack.service = token


def test_dataset_grant_reaches_one_table_and_raw_stays_refused_for_the_default_group(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    refused = stack.call_raw(
        grace["access_token"], "sql-exec", {"sql": "SELECT id FROM raw_shop.events"}
    )
    assert refused.get("isError"), refused
    stack.admin(stack.ada, "groups/create", {"name": "sales"})
    stack.admin(stack.ada, "groups/member", {"group": "sales", "id": grace["id"], "member": True})
    stack.admin(stack.ada, "grant", {"group": "sales", "kind": "dataset", "target": "events"})
    granted = stack.admin(stack.ada, "grants")["data"]
    assert any(g["group"] == "sales" and g["kind"] == "dataset" for g in granted), granted
    assert rows(sql(stack, grace["access_token"], "SELECT count(*) FROM raw_shop.events")) == [[2]]
    neighbour = stack.call_raw(
        grace["access_token"], "sql-exec", {"sql": "SELECT id FROM raw_shop.carts"}
    )
    assert neighbour.get("isError"), neighbour


def test_schema_grant_reaches_tables_created_after_it_and_revoke_removes_it(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    stack.admin(stack.ada, "grant", {"group": "sales", "kind": "schema", "target": "raw_shop"})
    stack.pg.sql(
        "CREATE TABLE raw_shop.late (id int); INSERT INTO raw_shop.late VALUES (9)", user="loader"
    )
    assert rows(sql(stack, grace["access_token"], "SELECT id FROM raw_shop.late")) == [[9]]
    stack.admin(stack.ada, "revoke", {"group": "sales", "kind": "schema", "target": "raw_shop"})
    stack.admin(stack.ada, "revoke", {"group": "sales", "kind": "dataset", "target": "events"})
    refused = stack.call_raw(
        grace["access_token"], "sql-exec", {"sql": "SELECT id FROM raw_shop.late"}
    )
    assert refused.get("isError"), refused


def test_denied_column_vanishes_from_describe_and_search_but_stays_selectable(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    token = grace["access_token"]
    stack.admin(stack.ada, "denied/set", {"group": "default", "columns": ["orders.customer_email"]})
    described = json.dumps(stack.call(token, "describe", {"dataset": "orders"}))
    assert "customer_email" not in described and "order_date" in described
    found = json.dumps(stack.call(token, "search", {"query": "email"}))
    assert "customer_email" not in found
    assert rows(sql(stack, token, "SELECT count(customer_email) FROM marts.orders")) == [[2]]


def test_warehouse_row_policy_applies_unchanged(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    answer = sql(stack, grace["access_token"], "SELECT DISTINCT region FROM marts.orders")
    assert rows(answer) == [["eu"]]
    assert "eu" not in json.dumps(answer.get("sql", "")).replace("SELECT DISTINCT region", "")


@pytest.mark.parametrize("role", ["impersonator", "provisioner"])
@pytest.mark.parametrize("db", ["airflow", "mcp_state"])
def test_server_roles_are_refused_on_airflow_and_mcp_state(stack, role, db):
    password = stack.pg.passwords[role.upper() + "_PASSWORD"]
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        stack.pg.connect(role, password, db).close()


def test_log_line_carries_the_session_user_and_the_person(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    stack.pg.sql("ALTER SYSTEM SET log_statement = 'all'", db="postgres")
    stack.pg.sql("SELECT pg_reload_conf()", db="postgres")
    try:
        sql(stack, grace["access_token"], "SELECT 'log-line-probe'")
        wanted = f"impersonator@warehouse app={grace['warehouse_name']}"
        deadline = time.time() + 10
        while wanted not in stack.pg.logs() and time.time() < deadline:
            time.sleep(0.5)
        lines = stack.pg.logs().splitlines()
        # The executed statement opens with its receipt comment, so the probe text sits on the
        # continuation line after the prefixed one.
        statements = [
            line
            for line, following in zip(lines, lines[1:], strict=False)
            if "@warehouse" in line and "statement:" in line and "log-line-probe" in following
        ]
        assert statements and all(wanted in line for line in statements), statements
    finally:
        stack.pg.sql("ALTER SYSTEM RESET log_statement", db="postgres")
        stack.pg.sql("SELECT pg_reload_conf()", db="postgres")


def test_tool_lists_per_principal_and_every_admin_read_answers(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    personal = set(stack.tools(grace["access_token"]))
    assert PERSON_TOOLS <= personal and not personal & ADMIN_TOOLS
    assert set(stack.tools(stack.service)) == personal
    administrative = set(stack.tools(stack.ada))
    assert PERSON_TOOLS | ADMIN_TOOLS <= administrative
    from vecten_mcp.analytics import VIEWS

    for tool in sorted(ADMIN_TOOLS - {"read_gap", "set_gap_status", "read_audit_row"}):
        if tool == "usage_view":
            for view in VIEWS:
                stack.call(stack.ada, tool, {"view": view})
        else:
            stack.call(stack.ada, tool, {})
    audit = json.dumps(stack.call(stack.ada, "search_audit", {"tool": "sql-exec"}))
    row_id = re.search(r'"id": "([0-9A-Z]{26})"', audit)[1]
    assert stack.call(stack.ada, "read_audit_row", {"row_id": row_id})


def test_metric_answer_feedback_folds_into_a_gap_the_admin_reads(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    token = grace["access_token"]
    request = {"metrics": ["total_amount"], "dimensions": ["orders.region"]}
    answer = stack.call(token, "query_metrics", request)
    assert rows(answer) == [["eu", 150]] or rows(answer) == [["eu", "150"]], answer
    receipt = answer["receipt"]["id"]
    report = stack.call(
        token,
        "report_feedback",
        {
            "question": "What is total amount by region?",
            "expected": "Net of refunds",
            "returned": "150",
            "tool_calls": [{"tool": "query_metrics", "arguments": request}],
            "category": "wrong definition",
            "raised_by": "user",
        },
    )
    assert report["status"] == "accepted"
    stack.runtime.feedback.fold()
    gaps = stack.call(stack.ada, "list_gaps", {})["data"]
    folded = [gap for gap in gaps if gap["category"] == "wrong definition"]
    assert len(folded) == 1, gaps
    assert folded[0]["names"] == ["total_amount"] and folded[0]["datasets"] == ["orders"]
    read = stack.call(stack.ada, "read_gap", {"gap_id": folded[0]["id"]})["data"]
    assert read["id"] == folded[0]["id"] and read["evidence"]["untrusted"] is True
    verified = stack.call(token, "verify_receipt", {"receipt_id": receipt})
    assert verified["status"] == "found" and verified["compiles_identically"] is True, verified
    assert verified["tool"] == "query_metrics"


def test_revoked_session_is_refused_and_loses_its_membership(stack):
    grace = person(stack, "grace", "grace@example.com", "Grace Hopper")
    sql(stack, grace["access_token"], "SELECT 1")
    revoked = stack.http.post(
        "/revoke",
        data={"token": grace["refresh_token"], "client_id": grace["client"]["client_id"]},
    )
    assert revoked.status_code == 200, revoked.text
    assert rpc(stack.http, grace["access_token"], "tools/list").status_code == 401
    assert "impersonator" not in members_of(stack, grace["warehouse_name"])
