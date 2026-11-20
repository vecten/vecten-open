"""Reconciliation lists the relations in core, marts and the declared areas that the last
full dbt build did not make, and drops none of them."""

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.state import StateStore
from vecten_shared.identity_setup import ACCESS_VERSION, GRANTS_VERSION, LISTING_VERSION


class Adapter:
    """Applies every grant and lists `present` as the warehouse's relations."""

    def __init__(self, present=(), version=ACCESS_VERSION):
        self.present, self.version = set(present), version
        self.ops, self.listed = [], 0

    def create_group(self, role):
        self.ops.append(("group", role))

    def access_version(self):
        return self.version

    def read_grants(self, roles):
        return set()

    def granted_roles(self, role):
        return set()

    def schema_grant(self, role, schema, grant=True):
        self.ops.append(("schema", role, schema, grant))

    def dataset_grant(self, role, source, grant=True):
        self.ops.append(("dataset", role, source, grant))

    def relations(self):
        self.listed += 1
        return set(self.present)

    def drop_statement(self, schema, name, kind):
        return f'DROP {kind} "{schema}"."{name}";'


def built(*places):
    return [{"database": "w", "schema": s, "name": n, "kind": "table"} for s, n in places]


def service(adapter, record, *, areas=("finance",)):
    access = AccessService(StateStore("sqlite://"), adapter, areas=areas, built=lambda: record)
    access.seed_defaults()
    return access


PRESENT = {
    ("marts", "orders", "TABLE"),
    ("marts", "finance_revenue", "TABLE"),
    ("marts_finance", "finance_revenue", "TABLE"),
    ("core_finance", "stale_invoice", "VIEW"),
    ("staging", "staging_crm_old", "VIEW"),
    ("marts_hr", "payroll", "TABLE"),
}
RECORD = built(("marts", "orders"), ("marts_finance", "finance_revenue"))


def test_each_relation_no_model_builds_is_listed_with_the_statement_that_drops_it():
    adapter = Adapter(PRESENT)
    access = service(adapter, RECORD)
    result = access.reconcile()
    assert access.health()["unbuilt"] == [
        {
            "schema": "core_finance",
            "relation": "stale_invoice",
            "kind": "VIEW",
            "statement": 'DROP VIEW "core_finance"."stale_invoice";',
        },
        {
            "schema": "marts",
            "relation": "finance_revenue",
            "kind": "TABLE",
            "statement": 'DROP TABLE "marts"."finance_revenue";',
        },
    ]
    assert result["complete"] and "unbuilt_error" not in access.health()
    assert not [op for op in adapter.ops if op[0] not in {"schema", "dataset", "group"}]


def test_a_dropped_relation_leaves_the_list_at_the_next_reconciliation():
    adapter = Adapter(PRESENT)
    access = service(adapter, RECORD)
    access.reconcile()
    adapter.present = {r for r in PRESENT if r[:2] != ("marts", "finance_revenue")}
    access.reconcile()
    assert [e["relation"] for e in access.health()["unbuilt"]] == ["stale_invoice"]


def test_names_are_compared_as_snowflake_stores_unquoted_identifiers():
    adapter = Adapter({("MARTS", "ORDERS", "TABLE"), ("MARTS_FINANCE", "OLD", "DYNAMIC TABLE")})
    access = service(adapter, RECORD)
    access.reconcile()
    assert access.health()["unbuilt"] == [
        {
            "schema": "MARTS_FINANCE",
            "relation": "OLD",
            "kind": "DYNAMIC TABLE",
            "statement": 'DROP DYNAMIC TABLE "MARTS_FINANCE"."OLD";',
        }
    ]


def test_without_a_current_record_no_relation_is_listed_or_read():
    adapter = Adapter(PRESENT)
    access = service(adapter, None)
    access.reconcile()
    assert access.health()["unbuilt"] == [] and adapter.listed == 0


def test_a_project_without_the_dbt_dag_lists_none():
    adapter = Adapter(PRESENT)
    access = AccessService(StateStore("sqlite://"), adapter, areas=("finance",))
    access.seed_defaults()
    access.reconcile()
    assert access.health()["unbuilt"] == [] and adapter.listed == 0


def test_an_unreadable_record_lists_none_and_logs(caplog):
    def unreadable():
        raise PermissionError("AccessDenied: platform/dbt/built_relations.json")

    adapter = Adapter(PRESENT)
    access = AccessService(StateStore("sqlite://"), adapter, areas=("finance",), built=unreadable)
    access.seed_defaults()
    result = access.reconcile()
    assert access.health()["unbuilt"] == [] and adapter.listed == 0
    assert result["complete"] and "AccessDenied" in caplog.text


def test_a_listing_the_warehouse_refuses_is_reported_and_grants_still_complete():
    class Refusing(Adapter):
        def relations(self):
            raise RuntimeError("SQL access control error: Insufficient privileges")

    access = service(Refusing(PRESENT), RECORD)
    result = access.reconcile()
    health = access.health()
    assert health["unbuilt"] == []
    assert "Insufficient privileges" in health["unbuilt_error"]
    assert result["complete"]


def test_procedures_of_the_grants_version_keep_areas_and_drift_but_list_nothing():
    adapter = Adapter(PRESENT, version=GRANTS_VERSION)
    access = service(adapter, RECORD)
    access.create_group("finance")
    access.grant("finance", "area", "finance")
    result = access.reconcile()
    health = access.health()
    assert result["complete"] and health["unbuilt"] == [] and adapter.listed == 0
    assert health["outdated"]["installed"] == GRANTS_VERSION
    assert health["outdated"]["required"] == ACCESS_VERSION
    assert "vecten mcp provisioner-script" in health["outdated"]["fix"]
    assert "no model builds" in health["outdated"]["fix"]


@pytest.mark.parametrize("version", [GRANTS_VERSION - 1, GRANTS_VERSION])
def test_the_grants_version_is_the_oldest_that_serves_area_grants(version):
    access = service(Adapter(version=version), RECORD)
    access.create_group("finance")
    if version < GRANTS_VERSION:
        with pytest.raises(ValueError, match="provisioner-script"):
            access.grant("finance", "area", "finance")
    else:
        assert access.grant("finance", "area", "finance")["kind"] == "area"


def test_version_six_keeps_relation_listing_while_requesting_the_procedure_update():
    adapter = Adapter(PRESENT, version=LISTING_VERSION)
    access = service(adapter, RECORD)
    assert access.reconcile()["complete"]
    health = access.health()
    assert adapter.listed and health["unbuilt"]
    assert "partial grant and revoke" in health["outdated"]["fix"]
