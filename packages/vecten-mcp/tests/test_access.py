import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.identity import Identity
from vecten_mcp.state import StateStore
from vecten_shared.identity_setup import ACCESS_VERSION


class Adapter:
    def __init__(self):
        self.ops = []

    def create_group(self, role):
        self.ops.append(("group", role))

    def drop_group(self, role):
        self.ops.append(("drop", role))

    def membership(self, role, name, grant=True):
        self.ops.append(("member", role, name, grant))

    def dataset_grant(self, role, source, grant=True):
        self.ops.append(("dataset", role, source, grant))

    def schema_grant(self, role, schema, grant=True):
        self.ops.append(("schema", role, schema, grant))

    def access_version(self):
        return ACCESS_VERSION

    def read_grants(self, roles):
        return set()


def test_group_grant_reconcile_and_deny_union():
    state = StateStore("sqlite://")
    adapter = Adapter()
    service = AccessService(state, adapter)
    p = state.create_principal(
        Identity("password", "jane@example.com", "jane@example.com", "Jane"),
        "jane",
        provisioned=True,
    )
    service.create_group("finance")
    service.membership("finance", p["id"])
    service.model_changed({"orders": "warehouse.marts.orders"})
    service.grant("finance", "dataset", "orders")
    assert ("dataset", "finance", "warehouse.marts.orders", True) in adapter.ops
    adapter.ops.clear()
    service.reconcile()
    assert ("dataset", "finance", "warehouse.marts.orders", True) in adapter.ops
    service.set_denied("default", ["orders.email"])
    service.set_denied("finance", ["orders.amount"])
    assert service.denied(p["id"]) == {"orders.email", "orders.amount"}
    service.revoke("finance", "dataset", "orders")
    assert ("dataset", "finance", "warehouse.marts.orders", False) in adapter.ops
    service.membership("finance", p["id"], False)
    assert state.groups_for(p["id"]) == {"default"}
    assert service.denied(p["id"]) == {"orders.email"}


def test_group_collision_and_default_deletion_refused():
    service = AccessService(StateStore("sqlite://"), Adapter())
    with pytest.raises(ValueError):
        service.create_group("default")
    with pytest.raises(ValueError):
        service.delete_group("default")
    with pytest.raises(ValueError):
        service.create_group("admin_finance")
    with pytest.raises(ValueError):
        service.grant("missing", "schema", "core")


def test_changed_dataset_source_revokes_old_then_grants_new():
    state, adapter = StateStore("sqlite://"), Adapter()
    service = AccessService(state, adapter)
    service.model_changed({"orders": "warehouse.marts.old_orders"})
    service.grant("default", "dataset", "orders")
    service.model_changed({"orders": "warehouse.marts.new_orders"})
    assert ("dataset", "analyst", "warehouse.marts.old_orders", False) in adapter.ops
    assert ("dataset", "analyst", "warehouse.marts.new_orders", True) in adapter.ops


def test_removed_dataset_revokes_old_grant_without_blocking_model_swap():
    state, adapter = StateStore("sqlite://"), Adapter()
    service = AccessService(state, adapter)
    service.model_changed({"orders": "warehouse.marts.orders"})
    service.grant("default", "dataset", "orders")
    service.model_changed({})
    assert ("dataset", "analyst", "warehouse.marts.orders", False) in adapter.ops
    assert not state.scan("applied_grants")


def test_bootstrap_grants_can_be_revoked_and_do_not_return_on_restart():
    state, adapter = StateStore("sqlite://"), Adapter()
    access = AccessService(state, adapter)
    access.seed_defaults()
    access.revoke("default", "schema", "core")
    assert ("schema", "analyst", "core", False) in adapter.ops
    restarted = AccessService(state, adapter)
    restarted.seed_defaults()
    assert all(row["target"] != "core" for row in restarted.list_grants())


class BlockedSchemaAdapter(Adapter):
    def schema_grant(self, role, schema, grant=True):
        raise NotImplementedError("owner helper is not configured")


def test_blocked_schema_does_not_skip_dataset_reconciliation():
    state, adapter = StateStore("sqlite://"), BlockedSchemaAdapter()
    access = AccessService(state, adapter)
    access.seed_defaults()
    access.datasets = {"orders": "warehouse.marts.orders"}
    access.grant("default", "dataset", "orders")
    adapter.ops.clear()
    result = access.reconcile()
    assert ("dataset", "analyst", "warehouse.marts.orders", True) in adapter.ops
    assert result["reconciled"] == 1
    assert len(result["failures"]) == 2
    assert not result["complete"]


def test_dataset_revoke_reports_completed_change_and_reconciliation_failures():
    state, adapter = StateStore("sqlite://"), BlockedSchemaAdapter()
    access = AccessService(state, adapter)
    access.seed_defaults()
    access.datasets = {"orders": "warehouse.marts.orders"}
    access.grant("default", "dataset", "orders")
    result = access.revoke("default", "dataset", "orders")
    assert result["target"] == "orders"
    assert len(result["reconciliation"]["failures"]) == 2
    assert ("dataset", "analyst", "warehouse.marts.orders", False) in adapter.ops
    assert all(row["kind"] != "dataset" for row in access.list_grants())


def test_failed_old_source_revocation_does_not_grant_new_source():
    class FailedRevoke(Adapter):
        def dataset_grant(self, role, source, grant=True):
            if not grant:
                raise RuntimeError("warehouse unavailable")
            super().dataset_grant(role, source, grant)

    state, adapter = StateStore("sqlite://"), FailedRevoke()
    access = AccessService(state, adapter)
    access.datasets = {"orders": "warehouse.marts.old"}
    access.grant("default", "dataset", "orders")
    adapter.ops.clear()
    result = access.model_changed({"orders": "warehouse.marts.new"})
    assert not result["complete"]
    assert not adapter.ops
    assert state.scan("applied_grants")[0][1]["source"] == "warehouse.marts.old"


@pytest.mark.parametrize("schema", ["staging", "snapshots"])
def test_explicit_intermediate_schema_grants(schema):
    state, adapter = StateStore("sqlite://"), Adapter()
    access = AccessService(state, adapter)
    access.create_group("engineering")
    access.grant("engineering", "schema", schema)
    assert ("schema", "engineering", schema, True) in adapter.ops
    access.revoke("engineering", "schema", schema)
    assert ("schema", "engineering", schema, False) in adapter.ops


class RefusingAdapter(Adapter):
    def dataset_grant(self, role, source, grant=True):
        from snowflake.connector.errors import ProgrammingError

        if self.refuse:
            raise ProgrammingError(msg="target is not a bounded reader role", errno=100132)
        super().dataset_grant(role, source, grant)


def test_a_failed_reconciliation_logs_and_keeps_each_target_and_error(caplog):
    import logging

    state, adapter = StateStore("sqlite://"), RefusingAdapter()
    adapter.refuse = False
    access = AccessService(state, adapter)
    access.datasets = {"orders": "warehouse.marts.orders"}
    access.grant("default", "dataset", "orders")
    state.remove("applied_grants", access._key("default", "dataset", "orders"))
    adapter.refuse = True
    with caplog.at_level(logging.WARNING, logger="vecten_mcp.access"):
        result = access.reconcile()
    (failure,) = result["failures"]
    assert failure["error"] == "ProgrammingError 100132: target is not a bounded reader role"
    assert "orders" in caplog.text and "target is not a bounded reader role" in caplog.text
    health = access.health()
    assert health["failures"] == [failure] and health["checked_at"] is not None
    adapter.refuse = False
    access.reconcile()
    assert access.health()["failures"] == []


def test_health_before_any_reconciliation_is_empty():
    assert AccessService(StateStore("sqlite://"), Adapter()).health() == {
        "checked_at": None,
        "failures": [],
        "added": [],
        "revoked": [],
        "drift": [],
    }
