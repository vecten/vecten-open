import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.administration import Administration, AdoptionRequired
from vecten_mcp.state import StateStore
from vecten_mcp.warehouse.base import Account
from vecten_shared.identity_setup import ACCESS_VERSION


class Adapter:
    def __init__(self):
        self.accounts, self.operations = {}, []
        self.fail = False

    def inspect_account(self, name):
        return self.accounts.get(name)

    def create_account(self, name, kind):
        if self.fail:
            raise ValueError("warehouse unavailable")
        self.accounts[name] = Account(name, can_adopt=True)
        self.operations.append(("create", name))

    def drop_account(self, name):
        self.operations.append(("drop", name))
        del self.accounts[name]

    def create_group(self, role):
        self.operations.append(("group", role))

    def drop_group(self, role):
        self.operations.append(("drop_group", role))

    def membership(self, role, name, grant=True):
        self.operations.append(("member", role, name, grant))

    def dataset_grant(self, role, source, grant=True):
        self.operations.append(("dataset", role, source, grant))

    def schema_grant(self, role, schema, grant=True):
        self.operations.append(("schema", role, schema, grant))

    def access_version(self):
        return ACCESS_VERSION

    def read_grants(self, roles):
        return set()


class Auth:
    def __init__(self, state):
        self.state = state
        self.disabled = []
        self.backend = None

    def disable_principal(self, pid):
        self.disabled.append(pid)
        self.state.update_principal(pid, enabled=False)

    def service_credentials(self, pid):
        return {"client_id": "fixture", "client_secret": "shown-once"}


@pytest.fixture
def admin():
    state = StateStore("sqlite://")
    adapter = Adapter()
    auth = Auth(state)
    access = AccessService(state, adapter)
    return Administration(state, adapter, auth, access), adapter


def test_creation_and_failed_provision_do_not_leave_principal(admin):
    service, adapter = admin
    result = service.create_person("jane@example.com", "Łukasz Żółć")
    assert result["warehouse_name"] == "lukasz_zolc"
    assert result["provisioned"] and not result["adopted"]
    assert ("member", "analyst", "lukasz_zolc", True) in adapter.operations
    adapter.fail = True
    with pytest.raises(ValueError):
        service.create_person("other@example.com", "Other Person")
    assert len(service.state.principals()) == 1


def test_adoption_requires_confirmation_scope_and_keeps_account_on_delete(admin):
    service, adapter = admin
    adapter.accounts["jan"] = Account(
        "jan", ("own_role",), can_adopt=False, adoption_sql="GRANT jan TO provisioner ..."
    )
    with pytest.raises(AdoptionRequired) as exc:
        service.create_person("jan@example.com", "Jan")
    assert exc.value.account.roles == ("own_role",)
    with pytest.raises(AdoptionRequired):
        service.create_person("jan@example.com", "Jan", adopt=True)
    adapter.accounts["jan"] = Account("jan", ("own_role",), can_adopt=True)
    p = service.create_person("jan@example.com", "Jan", adopt=True)
    service.delete_principal(p["id"])
    assert "jan" in adapter.accounts and ("member", "analyst", "jan", False) in adapter.operations


def test_admin_flag_has_no_warehouse_footprint_and_disable_revokes(admin):
    service, adapter = admin
    p = service.create_person("jane@example.com", "Jane")
    before = list(adapter.operations)
    service.set_admin(p["id"], True)
    assert adapter.operations == before
    service.disable(p["id"])
    assert service.auth.disabled == [p["id"]]
    assert not service.state.principal(p["id"])["enabled"]


def test_service_credentials_only_returned_by_create(admin):
    service, _ = admin
    created = service.create_service("reporting")
    assert (
        created["warehouse_name"] == "svc_reporting"
        and created["credentials"]["client_secret"] == "shown-once"
    )
    assert "shown-once" not in str(service.list_principals())
    with pytest.raises(ValueError):
        service.set_admin(created["id"], True)


def test_http_admin_requires_human_admin_and_records_every_call(admin):
    import warnings
    from types import SimpleNamespace

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
        )
        from starlette.testclient import TestClient
    from sqlalchemy import select
    from starlette.applications import Starlette

    from vecten_mcp.admin_routes import admin_routes
    from vecten_mcp.audit import AuditService
    from vecten_mcp.auth.service import Resolved
    from vecten_mcp.state import access_events

    service, _ = admin
    p = service.create_person("admin@example.com", "Admin Person", warehouse_override="jane")
    service.set_admin(p["id"], True)
    resolved = Resolved(service.state.principal(p["id"]), {"default"}, "client", "session", 999999)

    def resolve(token):
        if token != "fixture":
            raise ValueError("invalid")
        return resolved

    auth = SimpleNamespace(
        resolve=resolve,
        event=lambda pid, op, subject, outcome: service.state.access_event(
            pid, op, subject, outcome, now=1
        ),
    )
    runtime = SimpleNamespace(
        state=service.state, oauth=auth, administration=service, access=service.access
    )
    runtime.audit = AuditService(service.state, capacity=1, start=False)
    runtime.audit.enqueue({"id": "first", "at": 1})
    assert not runtime.audit.enqueue({"id": "overflow", "at": 1})
    with TestClient(Starlette(routes=admin_routes(runtime))) as client:
        assert client.get("/api/admin/users").status_code == 401
        headers = {"Authorization": "Bearer fixture"}
        assert client.get("/api/admin/audit-status", headers=headers).json()["data"] == {
            "queued": 1,
            "written": 0,
            "dropped": 1,
        }
        assert client.get("/api/admin/users", headers=headers).status_code == 200
        created = client.post(
            "/api/admin/users/create",
            headers=headers,
            json={"email": "new@example.com", "name": "New Person"},
        )
        assert created.status_code == 200, created.text
        resolved.principal["admin"] = False
        assert client.get("/api/admin/users", headers=headers).status_code == 403
        resolved.principal.update(admin=True, kind="service")
        assert client.get("/api/admin/users", headers=headers).status_code == 403
        resolved.principal.update(kind="human")
        assert client.get("/api/admin/gaps", headers=headers).json() == {"ok": True, "data": []}
    with service.state.transaction() as conn:
        rows = list(conn.execute(select(access_events)).mappings())
    assert len(rows) == 7 and sum(r["outcome"] == "denied" for r in rows) == 3
    runtime.audit.close()


def test_bootstrap_admin_can_receive_warehouse_identity(admin):
    service, adapter = admin
    pending = service.state.invite("owner@example.com", "Owner", "owner")
    service.state.update_principal(pending["id"], admin=True)
    result = service.create_person("owner@example.com", "Owner")
    assert result["id"] == pending["id"]
    assert result["admin"] and result["provisioned"]
    assert ("create", "owner") in adapter.operations


def http_admin(service):
    """A test client over the admin routes, signed in as an admin of `service`."""
    import warnings
    from types import SimpleNamespace

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
        )
        from starlette.testclient import TestClient
    from starlette.applications import Starlette

    from vecten_mcp.admin_routes import admin_routes
    from vecten_mcp.auth.service import Resolved

    p = service.create_person("admin@example.com", "Admin Person", warehouse_override="jane")
    service.set_admin(p["id"], True)
    resolved = Resolved(service.state.principal(p["id"]), {"default"}, "client", "session", 9)
    auth = SimpleNamespace(resolve=lambda token: resolved, event=lambda *args: None)
    runtime = SimpleNamespace(
        state=service.state, oauth=auth, administration=service, access=service.access, audit=None
    )
    return TestClient(Starlette(routes=admin_routes(runtime))), {"Authorization": "Bearer t"}


def test_a_grant_the_warehouse_refuses_answers_with_its_reason(admin, caplog):
    import logging

    import psycopg.errors

    service, adapter = admin
    service.access.datasets = {"orders": "warehouse.marts.orders"}

    def refuse(role, source, grant=True):
        raise psycopg.errors.InsufficientPrivilege("must be owner of table orders")

    adapter.dataset_grant = refuse
    client, headers = http_admin(service)
    with client, caplog.at_level(logging.ERROR, logger="vecten_mcp.admin_routes"):
        response = client.post(
            "/api/admin/grant",
            headers=headers,
            json={"group": "default", "kind": "dataset", "target": "orders"},
        )
    assert response.status_code == 503
    assert response.json()["error"] == (
        "administration failed: the warehouse refused the request (42501): "
        "must be owner of table orders"
    )
    assert "InsufficientPrivilege 42501" in caplog.text


def test_a_refused_request_answers_with_its_own_message(admin):
    service, _ = admin
    client, headers = http_admin(service)
    with client:
        response = client.post(
            "/api/admin/grant",
            headers=headers,
            json={"group": "default", "kind": "dataset", "target": "missing"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "unknown dataset"


def test_access_health_reads_the_last_reconciliation(admin):
    service, _ = admin
    client, headers = http_admin(service)
    with client:
        assert client.get("/api/admin/access-health", headers=headers).json()["data"] == {
            "checked_at": None,
            "failures": [],
            "added": [],
            "revoked": [],
            "drift": [],
            "database_future": [],
        }
        service.access.reconcile()
        data = client.get("/api/admin/access-health", headers=headers).json()["data"]
    assert data["failures"] == [] and data["checked_at"] is not None


def test_an_area_is_granted_and_listed_over_the_admin_api_and_an_undeclared_one_refused(admin):
    service, adapter = admin
    service.access.areas = frozenset({"finance"})
    service.access.create_group("finance")
    client, headers = http_admin(service)
    with client:
        granted = client.post(
            "/api/admin/grant",
            headers=headers,
            json={"group": "finance", "kind": "area", "target": "finance"},
        )
        refused = client.post(
            "/api/admin/grant",
            headers=headers,
            json={"group": "finance", "kind": "area", "target": "hr"},
        )
        listed = client.get("/api/admin/grants", headers=headers)
    assert granted.status_code == 200, granted.text
    assert ("schema", "finance", "core_finance", True) in adapter.operations
    assert ("schema", "finance", "marts_finance", True) in adapter.operations
    assert refused.status_code == 400
    assert "[areas.hr]" in refused.json()["error"]
    assert {"group": "finance", "kind": "area", "target": "finance"} in listed.json()["data"]
