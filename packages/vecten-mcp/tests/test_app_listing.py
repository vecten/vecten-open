"""The bound apps on the admin API, the admin tool and the panel, and their principals'
connection through `apps/connect`."""

import importlib.util
import re
from pathlib import Path

import pytest

from vecten_mcp.apps import APP_PRINCIPALS, AppScopes, BoundApp, app_url

spec = importlib.util.spec_from_file_location(
    "app_listing_surfaces_fixture", Path(__file__).with_name("test_admin_surfaces.py")
)
surfaces_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(surfaces_module)
panel, surfaces = surfaces_module.panel, surfaces_module.surfaces
builtins = surfaces_module.builtins
screens = surfaces_module.screens
api, api_read, cli, tool = (
    surfaces_module.api,
    surfaces_module.api_read,
    surfaces_module.cli,
    surfaces_module.tool,
)

OVERVIEW = BoundApp(
    extension="acme-sales",
    name="overview",
    title="Sales overview",
    kind="container",
    datasets=("orders",),
    groups=("sales", "default"),
    version="1.2.0",
    description="Weekly bookings by region",
    hostname="sales",
)
WAREHOUSE = BoundApp(
    extension="acme-sales",
    name="warehouse",
    title="Warehouse dashboard",
    kind="snowflake",
    datasets=("orders",),
    groups=("default",),
    version="1.2.0",
)


@pytest.fixture
def apps(surfaces):
    runtime = surfaces.runtime
    # The default group reads core and marts, as the warehouse's setup grants it.
    runtime.access.seed_defaults()
    runtime.access.create_group("sales")
    runtime.apps = AppScopes(
        runtime.state, {app.name: app for app in (OVERVIEW, WAREHOUSE)}, kind="postgres"
    )
    runtime.access.apps = runtime.apps
    runtime.access.admit()
    return surfaces


def listed(entries, name):
    return next(entry for entry in entries if entry["name"] == name)


@pytest.mark.parametrize(
    ("issuer", "label", "url"),
    [
        ("https://mcp.data.acme.example", "sales", "https://sales.data.acme.example/"),
        ("https://mcp.localhost:8443", "sales", "https://sales.localhost:8443/"),
        ("https://mcp.data.acme.example", "@", "https://data.acme.example/"),
        ("https://mcp.data.acme.example", None, None),
        ("https://analytics.acme.example", "sales", None),
    ],
)
def test_an_apps_url_sits_beside_the_mcp_hostname(issuer, label, url):
    assert app_url(issuer, label) == url


def test_the_admin_api_lists_each_bound_app_with_its_url_groups_and_state(apps):
    entries = api_read(apps, "apps")
    assert [entry["name"] for entry in entries] == ["overview", "warehouse"]
    overview = listed(entries, "overview")
    assert overview | {"failures": None} == {
        "app": "acme-sales.overview",
        "extension": "acme-sales",
        "version": "1.2.0",
        "name": "overview",
        "title": "Sales overview",
        "description": "Weekly bookings by region",
        "kind": "container",
        "url": "https://sales.example/",
        "groups": ["sales", "default"],
        "admitted": ["default"],
        "datasets": ["orders"],
        "state": "bound",
        "reason": None,
        "health": None,
        "binding": "[apps.acme-sales.overview] in vecten.toml",
        "principal": None,
        "failures": None,
    }
    # `sales` holds no grant on orders, so the gate leaves it out and access health says so.
    assert [failure["group"] for failure in overview["failures"]] == ["sales"]
    warehouse = listed(entries, "warehouse")
    assert warehouse["url"] is None and warehouse["state"] == "not published"
    assert "warehouse resources" in warehouse["reason"]
    assert "stage" in warehouse["reason"] and "app role" in warehouse["reason"]


def test_the_admin_tool_returns_what_the_admin_api_returns(apps):
    assert tool(apps, "list_apps")["data"] == api_read(apps, "apps") == cli(apps, "apps")


def test_the_listing_changes_no_binding(apps):
    headers = {"Authorization": "Bearer " + apps.token}
    response = apps.http.post("/api/admin/apps", headers=headers, json={"groups": ["sales"]})
    assert response.status_code == 404
    assert listed(api_read(apps, "apps"), "overview")["groups"] == ["sales", "default"]


def test_without_bound_apps_the_list_is_empty(surfaces):
    assert api_read(surfaces, "apps") == []
    assert tool(surfaces, "list_apps")["data"] == []


def test_connecting_creates_the_named_principal_in_exactly_the_bound_groups(apps):
    result = cli(apps, "apps/connect", {"name": "acme-sales.overview"})
    principal = result["principal"]
    assert principal["warehouse_name"] == "svc_app_overview"
    assert result["created"] is True
    assert result["groups"] == ["default", "sales"]
    state = apps.runtime.state
    assert state.groups_for(principal["id"]) == {"sales", "default"}
    assert state.get(APP_PRINCIPALS, principal["id"]) == {
        "extension": "acme-sales",
        "app": "overview",
    }
    oauth = apps.runtime.oauth
    credentials = result["credentials"]
    token = oauth.client_credentials(
        credentials["client_id"], credentials["client_secret"], oauth.resource
    )
    assert oauth.resolve(token["access_token"]).principal["id"] == principal["id"]
    entry = listed(api_read(apps, "apps"), "overview")
    assert entry["principal"] == {
        "id": principal["id"],
        "warehouse_name": "svc_app_overview",
        "marked": True,
        "enabled": True,
    }
    assert not any(
        f.get("reason") == "not marked as the app's principal" for f in entry["failures"]
    )


def test_a_principal_outside_default_leaves_the_default_group(apps):
    apps.runtime.apps.apps["overview"] = BoundApp(
        "acme-sales", "overview", "Sales overview", "container", ("orders",), ("sales",)
    )
    result = cli(apps, "apps/connect", {"name": "acme-sales.overview"})
    assert apps.runtime.state.groups_for(result["principal"]["id"]) == {"sales"}


def test_connecting_again_rotates_the_secret_and_keeps_the_principal(apps):
    oauth = apps.runtime.oauth
    first = cli(apps, "apps/connect", {"name": "acme-sales.overview"})
    session = oauth.client_credentials(
        first["credentials"]["client_id"], first["credentials"]["client_secret"], oauth.resource
    )
    second = cli(apps, "apps/connect", {"name": "acme-sales.overview"})
    assert second["created"] is False
    assert second["principal"]["id"] == first["principal"]["id"]
    assert second["credentials"]["client_secret"] != first["credentials"]["client_secret"]
    with pytest.raises(ValueError):
        oauth.client_credentials(
            first["credentials"]["client_id"], first["credentials"]["client_secret"], oauth.resource
        )
    with pytest.raises(ValueError):
        oauth.resolve(session["access_token"])
    oauth.client_credentials(
        second["credentials"]["client_id"], second["credentials"]["client_secret"], oauth.resource
    )


def test_connecting_follows_a_change_of_the_bound_groups(apps):
    first = cli(apps, "apps/connect", {"name": "acme-sales.overview"})
    apps.runtime.apps.apps["overview"] = BoundApp(
        "acme-sales", "overview", "Sales overview", "container", ("orders",), ("sales",)
    )
    cli(apps, "apps/connect", {"name": "acme-sales.overview"})
    assert apps.runtime.state.groups_for(first["principal"]["id"]) == {"sales"}


def refused(apps, data):
    headers = {"Authorization": "Bearer " + apps.token}
    response = apps.http.post("/api/admin/apps/connect", headers=headers, json=data)
    assert response.status_code == 400, response.text
    return response.json()["error"]


def unmarked_service(apps):
    """A service someone created by hand under the app principal's name, in `finance`."""
    runtime = apps.runtime
    created = runtime.administration.create_service("app overview")
    assert created["warehouse_name"] == "svc_app_overview"
    runtime.access.create_group("finance")
    runtime.access.membership("finance", created["id"])
    return created


def test_an_unmarked_principal_named_for_the_app_is_refused_without_adopt(apps):
    created = unmarked_service(apps)
    oauth, state = apps.runtime.oauth, apps.runtime.state
    error = refused(apps, {"name": "acme-sales.overview"})
    assert "svc_app_overview" in error and "--adopt" in error
    assert state.get(APP_PRINCIPALS, created["id"]) is None
    assert state.groups_for(created["id"]) == {"default", "finance"}
    credentials = created["credentials"]
    oauth.client_credentials(credentials["client_id"], credentials["client_secret"], oauth.resource)


def test_an_unmarked_principal_named_for_the_app_is_taken_over_with_adopt(apps):
    created = unmarked_service(apps)
    result = cli(apps, "apps/connect", {"name": "acme-sales.overview", "adopt": True})
    assert result["principal"]["id"] == created["id"] and result["created"] is False
    state = apps.runtime.state
    assert state.get(APP_PRINCIPALS, created["id"])["app"] == "overview"
    assert state.groups_for(created["id"]) == {"sales", "default"}


def test_a_principal_marked_for_another_app_is_always_refused(apps):
    created = unmarked_service(apps)
    marker = {"extension": "acme-other", "app": "overview"}
    apps.runtime.state.put(APP_PRINCIPALS, created["id"], marker)
    for data in ({"name": "acme-sales.overview"}, {"name": "acme-sales.overview", "adopt": True}):
        assert "acme-other.overview" in refused(apps, data)
    assert apps.runtime.state.get(APP_PRINCIPALS, created["id"]) == marker
    assert apps.runtime.state.groups_for(created["id"]) == {"default", "finance"}


def test_adopting_a_warehouse_account_with_roles_beyond_the_bound_groups_is_refused(apps):
    from vecten_mcp.warehouse.base import Account

    accounts = apps.adapter.accounts
    accounts["svc_app_overview"] = Account("svc_app_overview", ("PII_READER",), can_adopt=True)
    error = refused(apps, {"name": "acme-sales.overview", "adopt": True})
    assert "PII_READER" in error
    assert not [
        p for p in apps.runtime.state.principals() if p["warehouse_name"] == "svc_app_overview"
    ]


def test_adopting_a_warehouse_account_holding_only_bound_group_roles_is_allowed(apps):
    from vecten_mcp.warehouse.base import Account

    accounts = apps.adapter.accounts
    accounts["svc_app_overview"] = Account(
        "svc_app_overview", ("SALES", "ANALYST", "PUBLIC"), can_adopt=True
    )
    result = cli(apps, "apps/connect", {"name": "acme-sales.overview", "adopt": True})
    assert result["created"] is True


def test_an_existing_warehouse_account_without_adopt_asks_for_adoption(apps):
    from vecten_mcp.warehouse.base import Account

    apps.adapter.accounts["svc_app_overview"] = Account("svc_app_overview", (), can_adopt=True)
    headers = {"Authorization": "Bearer " + apps.token}
    response = apps.http.post(
        "/api/admin/apps/connect", headers=headers, json={"name": "acme-sales.overview"}
    )
    assert response.status_code == 409


def test_the_screen_names_adopt_for_an_unmarked_principal(apps):
    unmarked_service(apps)
    page = screens.text(apps.http.get("/admin/apps"))
    assert "vecten app connect acme-sales.overview --env <env> --adopt" in page


def test_app_text_on_the_screen_is_escaped(apps):
    apps.runtime.apps.apps["overview"] = BoundApp(
        "acme-sales",
        "overview",
        "<script>alert(1)</script>",
        "container",
        ("orders",),
        ("default",),
        description="<script>alert(2)</script>",
        hostname="sales",
    )
    body = apps.http.get("/admin/apps").text
    assert "<script>" not in body and "&lt;script&gt;alert(1)" in body


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("acme-sales.missing", "is not bound"),
        ("other.overview", "is not bound"),
        ("overview", "is not bound"),
        ("acme-sales.warehouse", "snowflake app"),
    ],
)
def test_connecting_refuses_an_app_without_a_container_binding(apps, name, reason):
    headers = {"Authorization": "Bearer " + apps.token}
    response = apps.http.post("/api/admin/apps/connect", headers=headers, json={"name": name})
    assert response.status_code == 400
    assert reason in response.json()["error"]
    assert not [
        p
        for p in apps.runtime.state.principals()
        if p["kind"] == "service" and p["warehouse_name"].startswith("svc_app_")
    ]


def test_connecting_refuses_a_bound_group_that_does_not_exist_before_any_change(apps):
    apps.runtime.apps.apps["overview"] = BoundApp(
        "acme-sales", "overview", "Sales overview", "container", ("orders",), ("finance",)
    )
    headers = {"Authorization": "Bearer " + apps.token}
    response = apps.http.post(
        "/api/admin/apps/connect", headers=headers, json={"name": "acme-sales.overview"}
    )
    assert response.status_code == 400
    assert "finance" in response.json()["error"]
    assert not [
        p for p in apps.runtime.state.principals() if p["warehouse_name"] == "svc_app_overview"
    ]


def test_connecting_refuses_a_disabled_principal(apps):
    first = cli(apps, "apps/connect", {"name": "acme-sales.overview"})
    apps.runtime.administration.disable(first["principal"]["id"])
    headers = {"Authorization": "Bearer " + apps.token}
    response = apps.http.post(
        "/api/admin/apps/connect", headers=headers, json={"name": "acme-sales.overview"}
    )
    assert response.status_code == 400
    assert "disabled" in response.json()["error"]


def test_connecting_is_not_an_agent_operation(apps):
    from vecten_mcp.admin_service import TOOL_OPERATIONS, WRITES

    assert "apps/connect" in WRITES and "apps/connect" not in TOOL_OPERATIONS
    assert "apps" in TOOL_OPERATIONS


def test_the_analytics_screen_lists_both_apps_read_only_and_names_the_binding(apps):
    response = apps.http.get("/admin/apps")
    assert response.status_code == 200, response.text
    page = screens.text(response)
    assert "Sales overview" in page and "Warehouse dashboard" in page
    assert "acme-sales 1.2.0" in page
    assert "https://sales.example/" in response.text
    assert re.search(r"Admitted groups\s+default", page)
    assert "[apps.acme-sales.overview] in vecten.toml" in page
    assert "not published" in page and "warehouse resources" in page
    assert "group sales may open acme-sales.overview but cannot read marts.orders" in page
    assert "vecten app connect acme-sales.overview" in page
    forms = re.findall(r'<form[^>]*action="([^"]+)"', response.text)
    assert forms == ["/admin/logout"]


def test_the_analytics_screen_is_in_the_menu(apps):
    assert '<a href="/admin/apps">Analytics</a>' in apps.http.get("/admin").text


def test_the_analytics_screen_without_apps_says_how_to_bind_one(surfaces):
    page = screens.text(surfaces.http.get("/admin/apps"))
    assert "No analytics app is bound" in page and "vecten.toml" in page


def connected(apps):
    return cli(apps, "apps/connect", {"name": "acme-sales.overview"})["principal"]


def member_grants(apps, name):
    return [(op[1], op[3]) for op in apps.adapter.operations if op[0] == "member" and op[2] == name]


def test_an_app_principal_may_join_no_group_outside_its_binding(apps):
    principal = connected(apps)
    apps.runtime.access.create_group("finance")
    headers = {"Authorization": "Bearer " + apps.token}
    data = {"group": "finance", "id": principal["id"], "member": True}
    response = apps.http.post("/api/admin/groups/member", headers=headers, json=data)
    assert response.status_code == 400
    assert "acme-sales.overview" in response.json()["error"]
    assert "finance" not in apps.runtime.state.groups_for(principal["id"])


def test_an_app_principal_may_leave_a_group_and_rejoin_a_bound_one(apps):
    principal = connected(apps)
    access = apps.runtime.access
    access.membership("sales", principal["id"], False)
    access.membership("sales", principal["id"], True)
    assert apps.runtime.state.groups_for(principal["id"]) == {"sales", "default"}


def test_the_warehouse_grants_an_app_principal_its_admitted_groups_roles_only(apps):
    principal = connected(apps)
    grants = member_grants(apps, "svc_app_overview")
    # default is admitted through its schema grant; sales holds no grant on orders.
    assert ("analyst", True) in grants
    assert ("sales", True) not in grants
    apps.runtime.access.grant("sales", "dataset", "orders")
    assert member_grants(apps, "svc_app_overview")[-1] == ("sales", True)
    apps.runtime.access.revoke("sales", "dataset", "orders")
    assert member_grants(apps, "svc_app_overview")[-1] == ("sales", False)
    assert apps.runtime.state.groups_for(principal["id"]) == {"sales", "default"}


def test_an_app_principal_whose_groups_differ_from_the_binding_is_in_access_health(apps):
    principal = connected(apps)
    apps.runtime.apps.apps["overview"] = BoundApp(
        "acme-sales", "overview", "Sales overview", "container", ("orders",), ("sales",)
    )
    entry = next(
        f
        for f in api_read(apps, "access-health")["apps"]
        if f.get("reason") == "groups differ from the binding"
    )
    assert entry["principal_id"] == principal["id"]
    assert (entry["groups"], entry["bound"]) == (["default", "sales"], ["sales"])
    assert "vecten app connect acme-sales.overview --env <env> --groups-only" in entry["message"]


def test_connecting_with_groups_only_resyncs_without_rotating(apps):
    oauth = apps.runtime.oauth
    first = cli(apps, "apps/connect", {"name": "acme-sales.overview"})
    apps.runtime.apps.apps["overview"] = BoundApp(
        "acme-sales", "overview", "Sales overview", "container", ("orders",), ("sales",)
    )
    result = cli(apps, "apps/connect", {"name": "acme-sales.overview", "rotate": False})
    assert result["credentials"] is None and result["groups"] == ["sales"]
    assert apps.runtime.state.groups_for(first["principal"]["id"]) == {"sales"}
    credentials = first["credentials"]
    oauth.client_credentials(credentials["client_id"], credentials["client_secret"], oauth.resource)


def test_groups_only_needs_an_existing_principal(apps):
    error = refused(apps, {"name": "acme-sales.overview", "rotate": False})
    assert "no principal" in error


def test_adopting_an_unmarked_principal_whose_account_holds_other_roles_is_refused(apps):
    from vecten_mcp.warehouse.base import Account

    created = unmarked_service(apps)
    apps.adapter.accounts["svc_app_overview"] = Account(
        "svc_app_overview", ("ANALYST", "FINANCE", "PII_READER"), can_adopt=True
    )
    error = refused(apps, {"name": "acme-sales.overview", "adopt": True})
    assert "PII_READER" in error and "FINANCE" not in error
    assert apps.runtime.state.get(APP_PRINCIPALS, created["id"]) is None


def failing_revoke(apps, monkeypatch, role):
    adapter = apps.adapter
    original = adapter.membership

    def membership(target, name, grant=True):
        if target == role and not grant and name == "svc_app_overview":
            raise RuntimeError("warehouse refused the revoke")
        return original(target, name, grant)

    monkeypatch.setattr(adapter, "membership", membership)
    return original


def flip_to_sales(apps):
    access = apps.runtime.access
    access.grant("sales", "dataset", "orders")
    access.revoke("default", "schema", "marts")
    assert apps.runtime.apps.admitted("overview") == {"sales"}


def admit_only(apps, *groups):
    from vecten_mcp.apps import ADMISSION

    apps.runtime.state.put(
        ADMISSION,
        "acme-sales.overview",
        {"admitted": list(groups), "excluded": [], "checked_at": 0},
    )


def test_a_role_whose_revoke_failed_stays_recorded_and_is_revoked_later(apps, monkeypatch):
    connected(apps)
    access, pid = apps.runtime.access, connected_id(apps)
    original = failing_revoke(apps, monkeypatch, "analyst")
    # One sync moves the principal from default's role to sales'; the revoke fails.
    admit_only(apps, "sales")
    with pytest.raises(RuntimeError):
        access.sync_app_roles(pid)
    assert access.held_roles(pid) == {"analyst", "sales"}
    monkeypatch.setattr(apps.adapter, "membership", original)
    admit_only(apps, "default")
    access.sync_app_roles(pid)
    assert member_grants(apps, "svc_app_overview")[-1] == ("sales", False)
    assert access.held_roles(pid) == {"analyst"}


def failing_grant(apps, monkeypatch, role, failures):
    """The first `failures` grants of `role` to the app principal fail; returns the grant
    attempts of `role`."""
    adapter, attempts = apps.adapter, []
    original = adapter.membership

    def membership(target, name, grant=True):
        if target == role and grant and name == "svc_app_overview":
            attempts.append(target)
            if len(attempts) <= failures:
                raise RuntimeError("warehouse unavailable")
        return original(target, name, grant)

    monkeypatch.setattr(adapter, "membership", membership)
    return attempts


def sync_failure(apps, pid):
    from vecten_mcp.access import APP_ROLES

    return (apps.runtime.state.get(APP_ROLES, pid) or {}).get("failed")


def test_a_grant_that_failed_is_retried_until_it_succeeds(apps, monkeypatch):
    connected(apps)
    access, pid = apps.runtime.access, connected_id(apps)
    attempts = failing_grant(apps, monkeypatch, "sales", failures=2)
    admit_only(apps, "default", "sales")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            access.sync_app_roles(pid)
        assert sync_failure(apps, pid) is not None
    access.sync_app_roles(pid)
    assert len(attempts) == 3
    assert member_grants(apps, "svc_app_overview")[-1] == ("sales", True)
    assert sync_failure(apps, pid) is None
    assert access.held_roles(pid) == {"analyst", "sales"}


def test_a_role_whose_grant_failed_is_revoked_once_it_is_not_wanted(apps, monkeypatch):
    connected(apps)
    access, pid = apps.runtime.access, connected_id(apps)
    failing_grant(apps, monkeypatch, "sales", failures=1)
    admit_only(apps, "default", "sales")
    with pytest.raises(RuntimeError):
        access.sync_app_roles(pid)
    admit_only(apps, "default")
    access.sync_app_roles(pid)
    assert member_grants(apps, "svc_app_overview")[-1] == ("sales", False)
    assert access.held_roles(pid) == {"analyst"}
    assert sync_failure(apps, pid) is None


def test_a_failed_role_sync_during_admission_is_logged(apps, monkeypatch, caplog):
    connected(apps)
    failing_revoke(apps, monkeypatch, "analyst")
    flip_to_sales(apps)
    assert "roles not synced" in caplog.text


def connected_id(apps):
    return next(
        p["id"]
        for p in apps.runtime.state.principals()
        if p["warehouse_name"] == "svc_app_overview"
    )


def test_a_failed_role_sync_is_in_access_health(apps, monkeypatch):
    connected(apps)
    failing_revoke(apps, monkeypatch, "analyst")
    flip_to_sales(apps)
    entry = next(
        f for f in api_read(apps, "access-health")["apps"] if f.get("reason") == "role sync failed"
    )
    assert entry["app"] == "acme-sales.overview" and "analyst" in entry["roles"]
    assert "vecten reconcile" in entry["message"]


def test_deleting_an_app_principal_revokes_every_recorded_role(apps, monkeypatch):
    connected(apps)
    failing_revoke(apps, monkeypatch, "analyst")
    flip_to_sales(apps)
    monkeypatch.undo()
    before = len(apps.adapter.operations)
    apps.runtime.administration.delete_principal(connected_id(apps))
    revoked = {
        op[1] for op in apps.adapter.operations[before:] if op[0] == "member" and op[3] is False
    }
    assert revoked == {"analyst", "sales"}


def test_deleting_a_group_revokes_its_role_from_an_app_principal(apps):
    connected(apps)
    apps.runtime.access.grant("sales", "dataset", "orders")
    assert member_grants(apps, "svc_app_overview")[-1] == ("sales", True)
    apps.runtime.access.delete_group("sales")
    assert ("sales", False) in member_grants(apps, "svc_app_overview")


def test_warehouse_roles_beyond_the_admitted_groups_are_in_access_health(apps):
    connected(apps)
    from vecten_mcp.warehouse.base import Account

    apps.adapter.accounts["svc_app_overview"] = Account(
        "svc_app_overview", ("ANALYST", "PII_READER", "PUBLIC")
    )
    entry = next(
        f
        for f in api_read(apps, "access-health")["apps"]
        if f.get("reason") == "warehouse roles beyond the admitted groups"
    )
    assert entry["roles"] == ["PII_READER"]
    assert "REVOKE" in entry["message"] or "revoke" in entry["message"]
