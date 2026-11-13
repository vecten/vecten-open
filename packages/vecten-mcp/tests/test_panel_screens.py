import importlib.util
import re
import time
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.administration import Administration
from vecten_mcp.warehouse.base import Account


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


session = load("panel_screens_session_fixture", "test_panel_session.py")
admin_api = load("panel_screens_admin_api_fixture", "test_admin_api.py")
builtins = session.builtins
invoke, content = session.fixture.invoke, session.fixture.content


@pytest.fixture
def panel(builtins):
    runtime = builtins.runtime
    adapter = admin_api.Adapter()
    runtime.access = AccessService(runtime.state, adapter)
    runtime.access.datasets = {"orders": "marts.orders"}
    runtime.administration = Administration(runtime.state, adapter, runtime.oauth, runtime.access)
    admin = session.person(builtins)
    assert session.panel_login(builtins.http).status_code == 303
    return SimpleNamespace(
        http=builtins.http, runtime=runtime, adapter=adapter, admin=admin, builtins=builtins
    )


def csrf(panel, path="/admin"):
    return session.hidden(panel.http.get(path), "csrf")


def post(panel, path, data, *, origin="/admin"):
    return panel.http.post(path, data={"csrf": csrf(panel, origin), **data}, follow_redirects=False)


def text(response):
    return unescape(re.sub(r"<[^>]+>", " ", response.text))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/admin", "Questions, last 7 days"),
        ("/admin/users", "Create a person"),
        ("/admin/access", "Deny list"),
        ("/admin/model", "revenue"),
        ("/admin/model/field?dataset=orders&field=amount", "semantic"),
        ("/admin/analytics", "Refusal reasons"),
        ("/admin/gaps", "Gaps"),
        ("/admin/audit", "Audit"),
        ("/admin/clients", "Connected clients"),
        ("/admin/settings", "audit_retention_days"),
    ],
)
def test_every_screen_renders_for_an_admin(panel, path, expected):
    response = panel.http.get(path)
    assert response.status_code == 200, response.text
    assert expected in text(response)


def test_provision_from_the_panel_shows_the_person_active(panel):
    response = post(panel, "/admin/users/create", {"email": "jane@example.com", "name": "Jane Doe"})
    assert response.status_code == 303
    assert ("create", "jane_doe") in panel.adapter.operations
    page = text(panel.http.get(response.headers["location"]))
    assert "Person created." in page
    row = next(line for line in page.splitlines() if "jane@example.com" in line)
    assert "jane_doe" in page and row


def test_existing_account_asks_for_adoption_before_any_change(panel):
    panel.adapter.accounts["jan"] = Account("jan", ("own_role",), "2026-10-01", can_adopt=True)
    response = post(panel, "/admin/users/create", {"email": "jan@example.com", "name": "Jan"})
    assert response.status_code == 200
    page = text(response)
    assert "already exists" in page and "own_role" in page
    assert panel.adapter.operations == []
    assert panel.runtime.state.by_email("jan@example.com") is None
    confirm = panel.http.post(
        "/admin/users/create",
        data={
            "csrf": session.hidden(response, "csrf"),
            "email": session.hidden(response, "email"),
            "name": session.hidden(response, "name"),
            "adopt": session.hidden(response, "adopt"),
        },
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    adopted = panel.runtime.state.by_email("jan@example.com")
    assert adopted["adopted"] and adopted["provisioned"]
    assert ("create", "jan") not in panel.adapter.operations


def test_adoption_that_needs_a_grant_prints_the_statement(panel):
    panel.adapter.accounts["jan"] = Account(
        "jan",
        ("own_role",),
        can_adopt=False,
        adoption_sql="GRANT jan TO provisioner WITH ADMIN OPTION",
    )
    response = post(panel, "/admin/users/create", {"email": "jan@example.com", "name": "Jan"})
    assert "GRANT jan TO provisioner WITH ADMIN OPTION" in text(response)


def test_service_principal_credentials_are_shown_once(panel):
    response = post(panel, "/admin/services/create", {"name": "reporting"})
    assert response.status_code == 200
    page = text(response)
    assert "svc_reporting" in page
    secret = re.search(r"Client secret\s+(\S+)", page)[1]
    assert secret
    assert secret not in panel.http.get("/admin/users").text


def test_admin_flag_membership_disable_and_groups_from_the_users_screen(panel):
    post(panel, "/admin/users/create", {"email": "jane@example.com", "name": "Jane"})
    jane = panel.runtime.state.by_email("jane@example.com")
    assert post(panel, f"/admin/users/{jane['id']}/admin", {"admin": "true"}).status_code == 303
    assert panel.runtime.state.principal(jane["id"])["admin"]
    assert post(panel, "/admin/groups/create", {"name": "finance"}).status_code == 303
    response = post(
        panel, "/admin/groups/member", {"group": "finance", "id": jane["id"], "member": "true"}
    )
    assert response.status_code == 303
    assert "finance" in panel.runtime.state.groups_for(jane["id"])
    assert post(panel, f"/admin/users/{jane['id']}/disable", {}).status_code == 303
    assert not panel.runtime.state.principal(jane["id"])["enabled"]
    assert post(panel, "/admin/groups/finance/delete", {}).status_code == 303
    assert panel.runtime.state.group("finance") is None


def test_grants_and_reconcile_from_the_access_screen(panel):
    response = post(
        panel,
        "/admin/access/grant",
        {"group": "default", "kind": "dataset", "target": "orders", "action": "grant"},
    )
    assert response.status_code == 303
    assert ("dataset", "analyst", "marts.orders", True) in panel.adapter.operations
    granted = text(panel.http.get("/admin/access"))
    assert re.search(r"default\s+dataset\s+orders", granted)
    response = post(
        panel,
        "/admin/access/grant",
        {"group": "default", "kind": "dataset", "target": "orders", "action": "revoke"},
    )
    assert response.status_code == 303
    assert post(panel, "/admin/access/reconcile", {}).status_code == 303


def test_denying_a_column_hides_it_from_the_next_describe(panel):
    response = post(
        panel, "/admin/access/denied", {"group": "default", "columns": "orders.email\r\n"}
    )
    assert response.status_code == 303
    result = content(invoke(panel.builtins, "describe", {"dataset": "orders"}))
    assert [f["name"] for f in result["datasets"][0]["fields"]] == ["amount"]


def test_triaging_a_gap_saves_and_lists_the_status(panel):
    panel.runtime.state.put("gaps", "g1", {"status": "open", "category": "wrong value"})
    response = post(panel, "/admin/gaps/g1", {"status": "triaged"}, origin="/admin/gaps/g1")
    assert response.status_code == 303
    listing = text(panel.http.get("/admin/gaps?status=all"))
    assert "g1" in listing and "triaged" in listing


UNREVIEWED_GAP = {
    "status": "open",
    "delivery_status": "merged without an approving review; the gap stays open",
    "pull_request": "https://github.com/example/project/pull/4",
    "unreviewed_fix": {
        "pull_request": "https://github.com/example/project/pull/4",
        "merge_commit": "b" * 40,
        "author": "solo-dev",
        "merged_at": "2026-10-20T10:00:00Z",
    },
}


def test_unreviewed_fix_says_why_and_offers_a_confirmation(panel):
    panel.runtime.state.put("gaps", "g1", UNREVIEWED_GAP)
    page = panel.http.get("/admin/gaps/g1")
    shown = text(page)
    assert "merged without an approving review; the gap stays open" in shown
    assert "solo-dev" in shown and "pull/4" in shown
    assert 'action="/admin/gaps/g1/confirm"' in page.text and "Confirm this fix" in shown


def test_confirming_an_unreviewed_fix_records_the_admin_and_drops_the_form(panel):
    panel.runtime.state.put("gaps", "g1", UNREVIEWED_GAP)
    response = post(panel, "/admin/gaps/g1/confirm", {}, origin="/admin/gaps/g1")
    assert response.status_code == 303
    confirmation = panel.runtime.state.get("gaps", "g1")["confirmation"]
    assert confirmation["merge_commit"] == "b" * 40
    page = panel.http.get("/admin/gaps/g1")
    assert confirmation["email"] in text(page)
    assert 'action="/admin/gaps/g1/confirm"' not in page.text


def test_gap_without_an_unreviewed_fix_offers_no_confirmation(panel):
    panel.runtime.state.put("gaps", "g1", {"status": "open"})
    assert 'action="/admin/gaps/g1/confirm"' not in panel.http.get("/admin/gaps/g1").text


def test_gap_evidence_is_escaped_and_marked_untrusted(panel):
    panel.runtime.state.put(
        "gaps", "g1", {"status": "open", "question": "<script>alert(1)</script>"}
    )
    page = panel.http.get("/admin/gaps/g1").text
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "untrusted" in page


def test_refused_row_shows_the_sql_call_that_followed(panel):
    now = time.time()
    audit = panel.runtime.audit
    audit.write(
        {
            "id": "R1",
            "at": now,
            "tool": "query_metrics",
            "principal_id": panel.admin["id"],
            "resolution": "refused",
            "refusal_reason": "unknown_metric",
            "request_chain_id": "R1",
        }
    )
    audit.write(
        {
            "id": "S1",
            "at": now + 1,
            "tool": "sql-exec",
            "principal_id": panel.admin["id"],
            "resolution": "agent_sql",
            "request_chain_id": "R1",
            "sql": "SELECT 1",
        }
    )
    listing = text(panel.http.get("/admin/audit?resolution=refused"))
    assert "R1" in listing and "S1" not in listing
    detail = text(panel.http.get("/admin/audit/R1"))
    assert "S1" in detail and "sql-exec" in detail and "SELECT 1" in detail


def test_audit_filters_accept_dates(panel):
    response = panel.http.get("/admin/audit?from=2026-10-01&to=2026-10-20&tool=sql-exec")
    assert response.status_code == 200
    assert panel.http.get("/admin/audit?from=yesterday").status_code == 400


def test_changing_retention_drives_audit_expiry(panel, monkeypatch):
    response = post(
        panel,
        "/admin/settings",
        {"key": "audit_retention_days", "value": "30"},
        origin="/admin/settings",
    )
    assert response.status_code == 303
    assert "30" in text(panel.http.get("/admin/settings"))
    calls = []
    monkeypatch.setattr(panel.runtime.state, "maintain_audit", calls.append)
    panel.runtime.last_audit_maintenance = 0
    panel.runtime.tick()
    assert calls == [30]


def test_redaction_toggle_is_saved_as_a_boolean(panel):
    post(panel, "/admin/settings", {"key": "redact", "value": "true"}, origin="/admin/settings")
    assert panel.runtime.state.get("settings", "redact") == {"value": True}


def test_reconcile_drift_is_chosen_from_its_two_values(panel):
    page = panel.http.get("/admin/settings").text
    assert '<option value="report"' in page and '<option value="revoke"' in page
    post(
        panel,
        "/admin/settings",
        {"key": "reconcile_drift", "value": "report"},
        origin="/admin/settings",
    )
    assert panel.runtime.state.get("settings", "reconcile_drift") == {"value": "report"}
    response = post(
        panel,
        "/admin/settings",
        {"key": "reconcile_drift", "value": "ignore"},
        origin="/admin/settings",
    )
    assert response.status_code == 400


def test_invalid_setting_is_refused(panel):
    response = post(
        panel, "/admin/settings", {"key": "row_cap", "value": "0"}, origin="/admin/settings"
    )
    assert response.status_code == 400


def test_field_view_names_the_source_and_skill_and_offers_no_edit(panel):
    page = panel.http.get("/admin/model/field?dataset=orders&field=amount")
    assert "osi/datasets.json: dataset orders, field amount" in text(page)
    assert "semantic" in text(page)
    forms = re.findall(r'<form[^>]*action="([^"]+)"', page.text)
    assert forms == ["/admin/logout"]


def test_model_view_shows_waiting_for_build(panel):
    panel.runtime.model._health["status"] = "shipped, waiting for build"
    page = text(panel.http.get("/admin/model"))
    assert "shipped, waiting for build" in page
    forms = re.findall(r'<form[^>]*action="([^"]+)"', panel.http.get("/admin/model").text)
    assert forms == ["/admin/logout"]


def test_connected_clients_list_people_across_sessions(panel):
    page = text(panel.http.get("/admin/clients"))
    assert "Admin" in page and "Vecten admin panel" in page


def test_unknown_field_is_a_refusal_not_a_crash(panel):
    assert panel.http.get("/admin/model/field?dataset=orders&field=nope").status_code == 400


def test_panel_actions_are_access_events(panel):
    from sqlalchemy import select

    from vecten_mcp.state import access_events

    post(panel, "/admin/groups/create", {"name": "finance"})
    with panel.runtime.state.transaction() as conn:
        operations = {row[0] for row in conn.execute(select(access_events.c.operation))}
    assert {"panel/groups/create", "panel/login"} <= operations
