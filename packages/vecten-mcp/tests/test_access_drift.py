"""Reconciliation reads what each managed role holds and revokes read grants the state lacks."""

import logging

from vecten_mcp.access import AccessService
from vecten_mcp.state import StateStore
from vecten_shared.identity_setup import ACCESS_VERSION


def full(grant):
    """A grant as `read_grants` returns it: `(role, kind, schema, relation, object)`; a test
    may name it without the object, which is then the schema or a table."""
    if len(grant) == 5:
        return grant
    return (*grant, "schema" if grant[1] == "usage" else "table")


class Adapter:
    """Holds read grants and applies grants to them; a schema-level revoke, like READ_GRANT's,
    reaches only tables and views."""

    def __init__(self, held=(), version=ACCESS_VERSION):
        self.held, self.version = set(held), version
        self.ops, self.reads = [], []

    def create_group(self, role):
        self.ops.append(("group", role))

    def access_version(self):
        return self.version

    def read_grants(self, roles):
        self.reads.append(set(roles))
        return {full(grant) for grant in self.held if grant[0] in roles}

    def schema_grant(self, role, schema, grant=True):
        self.ops.append(("schema", role, schema, grant))
        if grant:
            self.held |= {(role, "usage", schema, None), (role, "future", schema, None)}
        else:
            self.held = {
                g
                for g in self.held
                if not (
                    g[0] == role and g[2] == schema and full(g)[4] in {"schema", "table", "view"}
                )
            }

    def dataset_grant(self, role, source, grant=True):
        self.ops.append(("dataset", role, source, grant))
        schema, relation = source.split(".")[-2:]
        pair = {(role, "usage", schema, None), (role, "select", schema, relation)}
        if grant:
            self.held |= pair
        else:
            self.held = {g for g in self.held if full(g)[:4] != (role, "select", schema, relation)}


def service(adapter, *, seeded=True):
    access = AccessService(StateStore("sqlite://"), adapter)
    if seeded:
        access.seed_defaults()
    return access


def test_a_revoked_default_schema_restored_by_an_older_deploy_is_revoked_again():
    adapter = Adapter()
    access = service(adapter)
    access.revoke("default", "schema", "marts")
    adapter.held |= {("analyst", "usage", "marts", None), ("analyst", "future", "marts", None)}
    adapter.ops.clear()
    result = access.reconcile()
    assert ("schema", "analyst", "marts", False) in adapter.ops
    assert not any(op[2] == "marts" and op[3] for op in adapter.ops if op[0] == "schema")
    revoked = access.health()["revoked"]
    assert sorted((r["group"], r["privilege"], r["schema"]) for r in revoked) == [
        ("default", "FUTURE SELECT", "marts"),
        ("default", "USAGE", "marts"),
    ]
    assert result["complete"] and access.health()["drift"] == []


def test_a_grant_outside_the_scope_or_to_an_unmanaged_role_is_never_touched():
    adapter = Adapter(
        held={
            ("analyst", "select", "reference", "countries"),
            ("analyst", "usage", "reference", None),
            ("analyst", "usage", "vecten_access", None),
            ("client_role", "usage", "marts", None),
        }
    )
    access = service(adapter)
    access.reconcile()
    assert adapter.reads and all(roles <= {"analyst"} for roles in adapter.reads)
    assert not [op for op in adapter.ops if not op[3]]
    assert access.health()["revoked"] == []


def test_every_managed_role_is_read_and_a_hand_grant_in_a_raw_schema_is_revoked():
    adapter = Adapter()
    access = service(adapter)
    access.create_group("finance")
    adapter.held |= {("finance", "select", "raw_hubspot", "deals")}
    access.reconcile()
    assert {"analyst"} in adapter.reads and {"finance"} in adapter.reads
    assert ("dataset", "finance", "raw_hubspot.deals", False) in adapter.ops
    [entry] = access.health()["revoked"]
    assert entry == {
        "group": "finance",
        "role": "finance",
        "privilege": "SELECT",
        "object": "table",
        "schema": "raw_hubspot",
        "relation": "deals",
    }


def test_report_mode_lists_the_drift_and_revokes_nothing(caplog):
    adapter = Adapter()
    access = service(adapter)
    access.state.put("settings", "reconcile_drift", {"value": "report"})
    access.create_group("finance")
    adapter.held |= {("finance", "usage", "staging", None), ("finance", "future", "staging", None)}
    with caplog.at_level(logging.INFO, logger="vecten_mcp.access"):
        access.reconcile()
    assert not [op for op in adapter.ops if op[0] != "group" and not op[3]]
    health = access.health()
    assert health["revoked"] == []
    assert {(d["group"], d["privilege"], d["schema"]) for d in health["drift"]} == {
        ("finance", "USAGE", "staging"),
        ("finance", "FUTURE SELECT", "staging"),
    }
    assert "staging" in caplog.text and "finance" in caplog.text


def test_drift_is_revoked_before_the_desired_grants_are_applied_again():
    adapter = Adapter()
    access = service(adapter)
    access.revoke("default", "schema", "marts")
    access.datasets = {"orders": "warehouse.marts.orders"}
    access.grant("default", "dataset", "orders")
    adapter.held |= {("analyst", "future", "marts", None)}
    adapter.ops.clear()
    access.reconcile()
    revoke = adapter.ops.index(("schema", "analyst", "marts", False))
    grant = adapter.ops.index(("dataset", "analyst", "warehouse.marts.orders", True))
    assert revoke < grant
    assert ("analyst", "select", "marts", "orders") in adapter.held


def test_a_dataset_grants_usage_and_select_are_not_drift_but_a_neighbour_is():
    adapter = Adapter()
    access = service(adapter)
    access.revoke("default", "schema", "marts")
    access.datasets = {"orders": "warehouse.marts.orders"}
    access.grant("default", "dataset", "orders")
    adapter.held |= {("analyst", "select", "marts", "payroll")}
    adapter.ops.clear()
    access.reconcile()
    revokes = [op for op in adapter.ops if not op[3]]
    assert revokes == [("dataset", "analyst", "marts.payroll", False)]


def test_a_grant_missing_from_the_warehouse_is_listed_as_added():
    adapter = Adapter()
    access = service(adapter)
    access.reconcile()
    added = access.health()["added"]
    assert sorted((a["group"], a["kind"], a["target"]) for a in added) == [
        ("default", "schema", "core"),
        ("default", "schema", "marts"),
    ]
    access.reconcile()
    assert access.health()["added"] == []


def test_an_older_procedure_keeps_grants_reconciling_and_names_the_script():
    adapter = Adapter(held={("analyst", "usage", "staging", None)}, version=ACCESS_VERSION - 1)
    access = service(adapter)
    result = access.reconcile()
    assert adapter.reads == []
    assert ("schema", "analyst", "core", True) in adapter.ops
    assert not [op for op in adapter.ops if not op[3]]
    health = access.health()
    assert health["outdated"]["installed"] == ACCESS_VERSION - 1
    assert health["outdated"]["required"] == ACCESS_VERSION
    assert "vecten mcp provisioner-script" in health["outdated"]["fix"]
    assert result["complete"]


def test_a_failed_read_is_a_failure_and_the_grants_still_apply():
    class Unreadable(Adapter):
        def read_grants(self, roles):
            raise RuntimeError("warehouse unavailable")

    adapter = Unreadable()
    access = service(adapter)
    result = access.reconcile()
    assert ("schema", "analyst", "core", True) in adapter.ops
    [failure] = result["failures"]
    assert failure["operation"] == "read" and "warehouse unavailable" in failure["error"]


def test_a_failed_drift_revoke_is_a_failure_naming_the_place():
    class Refusing(Adapter):
        def schema_grant(self, role, schema, grant=True):
            if not grant:
                raise RuntimeError("no helper for staging")
            super().schema_grant(role, schema, grant)

    adapter = Refusing(held={("analyst", "usage", "staging", None)})
    access = service(adapter)
    result = access.reconcile()
    [failure] = result["failures"]
    assert failure["group"] == "default" and failure["target"] == "staging"
    assert failure["kind"] == "drift" and failure["operation"] == "revoke"
    assert access.health()["revoked"] == []


def test_state_from_an_earlier_release_starts_by_reporting_drift():
    state, adapter = StateStore("sqlite://"), Adapter()
    earlier = AccessService(state, adapter)
    earlier.seed_defaults()
    state.remove("bootstrap", "drift_mode")
    state.remove("settings", "reconcile_drift")
    adapter.held |= {("analyst", "select", "staging", "orders")}
    upgraded = AccessService(state, adapter)
    upgraded.seed_defaults()
    upgraded.reconcile()
    assert state.get("settings", "reconcile_drift") == {"value": "report"}
    assert not [op for op in adapter.ops if not op[3]]
    assert [d["relation"] for d in upgraded.health()["drift"]] == ["orders"]


def test_a_fresh_state_revokes_and_an_admins_choice_survives_a_restart():
    state, adapter = StateStore("sqlite://"), Adapter()
    access = AccessService(state, adapter)
    access.seed_defaults()
    assert state.get("settings", "reconcile_drift") is None
    state.put("settings", "reconcile_drift", {"value": "report"})
    AccessService(state, adapter).seed_defaults()
    assert state.get("settings", "reconcile_drift") == {"value": "report"}
    state.put("settings", "reconcile_drift", {"value": "revoke"})
    AccessService(state, adapter).seed_defaults()
    assert state.get("settings", "reconcile_drift") == {"value": "revoke"}


def test_a_future_grant_the_procedure_cannot_revoke_stays_listed_as_drift():
    adapter = Adapter()
    access = service(adapter)
    access.revoke("default", "schema", "marts")
    adapter.held |= {
        ("analyst", "usage", "marts", None),
        ("analyst", "future", "marts", None, "table"),
        ("analyst", "future", "marts", None, "dynamic_table"),
    }
    result = access.reconcile()
    health = access.health()
    assert {(r["privilege"], r["object"]) for r in health["revoked"]} == {
        ("USAGE", "schema"),
        ("FUTURE SELECT", "table"),
    }
    assert [(d["privilege"], d["object"]) for d in health["drift"]] == [
        ("FUTURE SELECT", "dynamic_table")
    ]
    [failure] = result["failures"]
    assert failure["kind"] == "drift" and failure["target"] == "marts"
    assert "still held" in failure["error"]


def test_a_relation_that_cannot_be_revoked_does_not_stop_the_others():
    class Refusing(Adapter):
        def dataset_grant(self, role, source, grant=True):
            if not grant and source.endswith(".broken"):
                raise RuntimeError("dataset relation does not exist")
            super().dataset_grant(role, source, grant)

    adapter = Refusing(
        held={("analyst", "select", "staging", "broken"), ("analyst", "select", "staging", "ok")}
    )
    access = service(adapter)
    result = access.reconcile()
    health = access.health()
    assert [r["relation"] for r in health["revoked"]] == ["ok"]
    assert [d["relation"] for d in health["drift"]] == ["broken"]
    [failure] = result["failures"]
    assert failure["target"] == "staging.broken" and "does not exist" in failure["error"]


def test_a_quoted_relation_name_is_listed_and_never_revoked_by_another_name():
    adapter = Adapter(held={("analyst", "select", "staging", '"Orders"')})
    access = service(adapter)
    access.reconcile()
    assert not [op for op in adapter.ops if not op[3]]
    assert [d["relation"] for d in access.health()["drift"]] == ['"Orders"']


def test_one_unreadable_role_does_not_stop_the_others():
    class OneUnreadable(Adapter):
        def read_grants(self, roles):
            if "finance" in roles:
                raise RuntimeError("finance cannot be read")
            return super().read_grants(roles)

    adapter = OneUnreadable(held={("analyst", "usage", "staging", None)})
    access = service(adapter)
    access.create_group("finance")
    result = access.reconcile()
    assert ("schema", "analyst", "staging", False) in adapter.ops
    [failure] = result["failures"]
    assert failure["operation"] == "read" and failure["group"] == "finance"


def test_database_level_future_grants_are_reported_and_kept():
    adapter = Adapter(held={("analyst", "future", None, None, "table")})
    access = service(adapter)
    access.reconcile()
    assert not [op for op in adapter.ops if not op[3]]
    assert access.health()["database_future"] == [
        {"group": "default", "role": "analyst", "privilege": "FUTURE SELECT", "object": "table"}
    ]
