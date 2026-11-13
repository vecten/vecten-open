"""Reconciliation reads what each managed role holds and revokes read grants the state lacks;
area grants are two schema grants applied and revoked together."""

import logging

import pytest

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

    def granted_roles(self, role):
        return set()

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


def service(adapter, *, seeded=True, areas=()):
    access = AccessService(StateStore("sqlite://"), adapter, areas=areas)
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


def test_an_area_grant_is_both_schema_grants_and_its_revoke_revokes_both():
    adapter = Adapter()
    access = service(adapter, areas={"finance"})
    access.create_group("finance")
    adapter.ops.clear()
    row = access.grant("finance", "area", "finance")
    assert row == {"group": "finance", "kind": "area", "target": "finance"}
    assert adapter.ops == [
        ("schema", "finance", "core_finance", True),
        ("schema", "finance", "marts_finance", True),
    ]
    assert row in access.list_grants()
    adapter.ops.clear()
    access.revoke("finance", "area", "finance")
    assert ("schema", "finance", "core_finance", False) in adapter.ops
    assert ("schema", "finance", "marts_finance", False) in adapter.ops
    assert not [g for g in access.list_grants() if g["kind"] == "area"]


def test_a_grant_of_an_undeclared_area_is_refused_naming_its_entry():
    adapter = Adapter()
    access = service(adapter, areas={"finance"})
    access.create_group("people")
    adapter.ops.clear()
    with pytest.raises(ValueError, match=r"\[areas\.hr\]"):
        access.grant("people", "area", "hr")
    with pytest.raises(ValueError, match=r"\[areas\.hr\]"):
        access.grant("people", "schema", "marts_hr")
    assert adapter.ops == []


def test_a_schema_grant_takes_one_schema_of_a_declared_area():
    adapter = Adapter()
    access = service(adapter, areas={"finance"})
    access.create_group("finance")
    adapter.ops.clear()
    access.grant("finance", "schema", "marts_finance")
    assert adapter.ops == [("schema", "finance", "marts_finance", True)]
    with pytest.raises(ValueError, match="convention schemas"):
        access.grant("finance", "schema", "reference")


def test_a_grant_of_an_unknown_kind_is_refused():
    access = service(Adapter())
    with pytest.raises(ValueError, match="dataset, schema or area"):
        access.grant("default", "database", "warehouse")


def test_an_older_procedure_refuses_area_grants_and_names_the_script():
    adapter = Adapter(version=ACCESS_VERSION - 1)
    access = service(adapter, areas={"finance"})
    access.create_group("finance")
    adapter.ops.clear()
    for kind, target in (("area", "finance"), ("schema", "core_finance")):
        with pytest.raises(ValueError, match="vecten mcp provisioner-script"):
            access.grant("finance", kind, target)
    assert adapter.ops == []


def test_an_older_procedure_refuses_a_dataset_grant_on_an_area_mart():
    adapter = Adapter(version=ACCESS_VERSION - 1)
    access = service(adapter, areas={"finance"})
    access.datasets = {"finance_revenue": "warehouse.marts_finance.finance_revenue"}
    access.create_group("finance")
    adapter.ops.clear()
    with pytest.raises(ValueError, match="vecten mcp provisioner-script"):
        access.grant("finance", "dataset", "finance_revenue")
    assert adapter.ops == []


def test_an_area_grant_recorded_before_the_script_rerun_waits_and_the_rest_reconcile():
    adapter = Adapter()
    access = service(adapter, areas={"finance"})
    access.create_group("finance")
    access.grant("finance", "area", "finance")
    adapter.version = ACCESS_VERSION - 1
    adapter.ops.clear()
    result = access.reconcile()
    assert ("schema", "analyst", "core", True) in adapter.ops
    assert not [op for op in adapter.ops if "finance" in op[2]]
    [failure] = result["failures"]
    assert (failure["group"], failure["kind"], failure["target"]) == ("finance", "area", "finance")
    assert "vecten mcp provisioner-script" in failure["error"]


def test_a_granted_area_is_not_drift_and_a_hand_grant_in_an_area_is_revoked():
    adapter = Adapter()
    access = service(adapter, areas={"finance"})
    access.create_group("finance")
    access.grant("finance", "area", "finance")
    adapter.held |= {
        ("analyst", "usage", "marts_finance", None),
        ("analyst", "select", "core_finance", "invoice"),
    }
    adapter.ops.clear()
    access.reconcile()
    revokes = sorted(op for op in adapter.ops if not op[3])
    assert revokes == [
        ("dataset", "analyst", "core_finance.invoice", False),
        ("schema", "analyst", "marts_finance", False),
    ]
    assert not access.health()["drift"]
    held = {g[:4] for g in adapter.held if g[0] == "finance"}
    assert ("finance", "future", "marts_finance", None) in held
    assert ("finance", "future", "core_finance", None) in held


def test_a_schema_named_like_an_undeclared_area_is_outside_the_scope():
    adapter = Adapter(held={("analyst", "usage", "marts_legacy", None)})
    access = service(adapter, areas={"finance"})
    access.reconcile()
    assert not [op for op in adapter.ops if not op[3]]
    assert access.health()["drift"] == []


def test_an_area_no_longer_declared_is_revoked_and_forgotten():
    state, adapter = StateStore("sqlite://"), Adapter()
    declared = AccessService(state, adapter, areas={"finance"})
    declared.seed_defaults()
    declared.create_group("finance")
    declared.datasets = {"finance_revenue": "warehouse.marts_finance.finance_revenue"}
    declared.grant("finance", "area", "finance")
    declared.grant("finance", "dataset", "finance_revenue")
    adapter.ops.clear()
    undeclared = AccessService(state, adapter)
    undeclared.datasets = dict(declared.datasets)
    result = undeclared.reconcile()
    assert ("schema", "finance", "core_finance", False) in adapter.ops
    assert ("schema", "finance", "marts_finance", False) in adapter.ops
    assert ("dataset", "finance", "warehouse.marts_finance.finance_revenue", False) in adapter.ops
    assert result["complete"]
    assert [g for g in undeclared.list_grants() if g["group"] == "finance"] == []
    assert not [k for k, row in state.scan("applied_grants") if row["group"] == "finance"]
    adapter.ops.clear()
    AccessService(state, adapter, areas={"finance"}).reconcile()
    assert not [op for op in adapter.ops if op[1] == "finance"]


def test_a_dataset_in_an_undeclared_area_is_refused():
    adapter = Adapter()
    access = service(adapter, areas=set())
    access.datasets = {"finance_revenue": "warehouse.marts_finance.finance_revenue"}
    adapter.ops.clear()
    with pytest.raises(ValueError, match=r"\[areas\.finance\]"):
        access.grant("default", "dataset", "finance_revenue")
    assert adapter.ops == []


def test_a_non_string_target_is_a_value_error():
    access = service(Adapter())
    for kind in ("schema", "area", "dataset"):
        with pytest.raises(ValueError):
            access.grant("default", kind, 123)


class RefusingRevoke(Adapter):
    """A warehouse that refuses to revoke: READ_GRANT throws while the target still reads."""

    def schema_grant(self, role, schema, grant=True):
        if not grant:
            self.ops.append(("schema", role, schema, grant))
            raise RuntimeError("the target still holds SELECT in D.MARTS after refusals")
        return super().schema_grant(role, schema, grant)


def test_a_refused_revoke_keeps_the_applied_grant_and_is_retried_as_a_failure():
    adapter = RefusingRevoke()
    access = service(adapter, seeded=False)
    access.create_group("finance")
    access.grant("finance", "schema", "staging")
    result = access.revoke("finance", "schema", "staging")
    assert result["reconciliation"]["complete"] is False
    [failure] = [f for f in result["reconciliation"]["failures"] if f["kind"] == "schema"]
    assert (failure["target"], failure["operation"]) == ("staging", "revoke")
    assert "still holds" in failure["error"]
    assert not access.list_grants()
    [(_, applied)] = list(access.state.scan("applied_grants"))
    assert applied["target"] == "staging"
    adapter.ops.clear()
    access.reconcile()
    assert ("schema", "finance", "staging", False) in adapter.ops
    assert [row["target"] for _, row in access.state.scan("applied_grants")] == ["staging"]


def test_a_half_applied_area_grant_is_rolled_back():
    class RefusingMarts(Adapter):
        def schema_grant(self, role, schema, grant=True):
            if grant and schema.startswith("marts_"):
                raise RuntimeError("no helper for marts_finance")
            return super().schema_grant(role, schema, grant)

    adapter = RefusingMarts()
    access = service(adapter, areas={"finance"})
    access.create_group("finance")
    adapter.ops.clear()
    with pytest.raises(RuntimeError, match="no helper"):
        access.grant("finance", "area", "finance")
    # The failing schema is revoked too: a refusal may come after its USAGE was granted.
    assert adapter.ops == [
        ("schema", "finance", "core_finance", True),
        ("schema", "finance", "core_finance", False),
        ("schema", "finance", "marts_finance", False),
    ]
    assert not [g for g in access.list_grants() if g["kind"] == "area"]


def test_a_half_applied_area_grant_that_cannot_be_rolled_back_is_recorded_for_revoking():
    class Stuck(Adapter):
        def schema_grant(self, role, schema, grant=True):
            if schema.startswith("marts_") or not grant:
                raise RuntimeError(f"refused on {schema}")
            return super().schema_grant(role, schema, grant)

    adapter = Stuck()
    access = service(adapter, areas={"finance"})
    access.create_group("finance")
    with pytest.raises(RuntimeError, match="marts_finance"):
        access.grant("finance", "area", "finance")
    leftovers = [row for _, row in access.state.scan("applied_grants") if row["kind"] == "area"]
    assert [row["source"] for row in leftovers] == [["core_finance", "marts_finance"]]
    result = access.reconcile()
    assert any(f["kind"] == "area" and f["operation"] == "revoke" for f in result["failures"])


class RefusingIceberg(Adapter):
    """A Snowflake account that refuses Iceberg table grants: READ_GRANT applies the other
    kinds and reports the refused one."""

    def schema_grant(self, role, schema, grant=True):
        super().schema_grant(role, schema, grant)
        return [f"FUTURE ICEBERG TABLES: unsupported feature 'ICEBERG' in {schema}"]


def test_a_kind_the_warehouse_refuses_is_a_failure_with_its_message_and_the_grant_holds():
    adapter = RefusingIceberg()
    access = service(adapter, seeded=False)
    access.create_group("finance")
    row = access.grant("finance", "schema", "marts")
    assert row["refused"] == [
        "marts: FUTURE ICEBERG TABLES: unsupported feature 'ICEBERG' in marts"
    ]
    assert {"group": "finance", "kind": "schema", "target": "marts"} in access.list_grants()
    result = access.reconcile()
    [failure] = result["failures"]
    assert (failure["group"], failure["kind"], failure["target"]) == ("finance", "schema", "marts")
    assert failure["operation"] == "grant" and "unsupported feature 'ICEBERG'" in failure["error"]
    assert result["reconciled"] == 1 and not result["complete"]
    assert access.health()["failures"] == [failure]


def test_a_failed_grant_again_of_a_held_area_rolls_nothing_back():
    class RefusingMartsLater(Adapter):
        refuse = False

        def schema_grant(self, role, schema, grant=True):
            if grant and self.refuse and schema.startswith("marts_"):
                raise RuntimeError("no helper for marts_finance")
            return super().schema_grant(role, schema, grant)

    adapter = RefusingMartsLater()
    access = service(adapter, areas={"finance"})
    access.create_group("finance")
    access.grant("finance", "area", "finance")
    adapter.refuse = True
    adapter.ops.clear()
    with pytest.raises(RuntimeError):
        access.grant("finance", "area", "finance")
    assert not [op for op in adapter.ops if not op[3]]
