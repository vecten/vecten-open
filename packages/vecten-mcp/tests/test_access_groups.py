"""A group's role can be named at creation, and a group can bind a role the client already has,
which the server prints the statement for, records once it is in place, and never manages."""

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.state import StateStore
from vecten_mcp.warehouse.base import RoleExists
from vecten_shared.identity_setup import ACCESS_VERSION


class Adapter:
    """Roles that exist, and the roles granted to each group role."""

    def __init__(self, existing=()):
        self.roles, self.granted, self.ops = set(existing), {}, []

    def create_group(self, role):
        if role in self.roles:
            raise RoleExists(role)
        self.roles.add(role)
        self.ops.append(("create", role))

    def drop_group(self, role):
        self.roles.discard(role)
        self.ops.append(("drop", role))

    def bind_statement(self, bound, role, grant=True):
        verb = "GRANT" if grant else "REVOKE"
        return f"{verb} {bound} {'TO' if grant else 'FROM'} {role}"

    def review_statement(self, bound):
        return f"SHOW {bound}"

    def holds_role(self, role, bound):
        self.ops.append(("holds", role, bound))
        return bound in self.granted.get(role, set())

    def schema_grant(self, role, schema, grant=True):
        self.ops.append(("schema", role, schema, grant))

    def dataset_grant(self, role, source, grant=True):
        self.ops.append(("dataset", role, source, grant))

    def membership(self, role, name, grant=True):
        self.ops.append(("member", role, name, grant))

    def access_version(self):
        return ACCESS_VERSION

    def read_grants(self, roles):
        return set()


def service(adapter=None):
    access = AccessService(StateStore("sqlite://"), adapter or Adapter())
    access.seed_defaults()
    return access


def test_a_group_takes_the_role_named_at_creation():
    access = service()
    assert access.create_group("finance", role="fin_readers") == {
        "name": "finance",
        "warehouse_role": "fin_readers",
    }
    assert access.adapter.ops == [("create", "fin_readers")]
    assert access.require_group("finance")["warehouse_role"] == "fin_readers"
    assert access.state.get("provisioner_registry", "fin_readers") == {"kind": "group"}


@pytest.mark.parametrize(
    "role", ["admin_x", "svc_x", "pg_read_all_data", "p_jane", "analyst", "Fin", "x-y", ""]
)
def test_a_role_name_outside_the_rule_is_refused(role):
    access = service()
    with pytest.raises(ValueError):
        access.create_group("finance", role=role)
    assert access.adapter.ops == [] and access.state.group("finance") is None


def test_a_derived_role_name_with_a_person_prefix_is_refused():
    access = service()
    with pytest.raises(ValueError, match="--role"):
        access.create_group("p_team")


def test_a_role_another_group_maps_to_is_refused():
    access = service()
    access.create_group("finance")
    with pytest.raises(ValueError, match="group finance"):
        access.create_group("board", role="finance")


def test_a_group_named_like_an_existing_role_is_refused_naming_both_ways_out():
    access = service(Adapter(existing={"finance"}))
    with pytest.raises(ValueError) as refused:
        access.create_group("finance")
    assert "--role" in str(refused.value) and "vecten group bind" in str(refused.value)
    assert access.state.group("finance") is None
    assert access.state.get("provisioner_registry", "finance") is None
    access.create_group("finance", role="fin_readers")
    assert access.require_group("finance")["warehouse_role"] == "fin_readers"


def test_binding_prints_the_statement_and_records_the_role_once_it_is_granted():
    adapter = Adapter(existing={"finance_analyst"})
    access = service(adapter)
    access.create_group("finance")
    pending = access.bind("finance", "finance_analyst")
    assert pending == {
        "group": "finance",
        "role": "finance",
        "bound_role": "finance_analyst",
        "confirmed": False,
        "statement": "GRANT finance_analyst TO finance",
        "review": "SHOW finance_analyst",
    }
    with pytest.raises(ValueError, match="GRANT finance_analyst TO finance"):
        access.confirm_binding("finance")
    adapter.granted["finance"] = {"finance_analyst"}
    assert access.confirm_binding("finance")["confirmed"] is True
    [group] = [g for g in access.list_groups() if g["name"] == "finance"]
    assert (group["bound_role"], group["binding"]) == ("finance_analyst", "confirmed")
    assert not [op for op in adapter.ops if op[0] in {"create", "drop"} and op[1] != "finance"]


@pytest.mark.parametrize(
    "bound",
    [
        "analyst",
        "finance",
        "board",
        "ACCOUNTADMIN",
        "sysadmin",
        "public",
        "loader",
        "provisioner",
        "pg_read_all_data",
        "admin_ops",
        "svc_etl",
        "p_jane",
        'x";drop',
    ],
)
def test_binding_a_managed_or_privileged_role_is_refused(bound):
    access = service()
    access.create_group("finance")
    access.create_group("board")
    with pytest.raises(ValueError):
        access.bind("finance", bound)
    assert access.state.get("bindings", "finance") is None


def test_the_default_group_binds_nothing():
    access = service()
    with pytest.raises(ValueError, match="default"):
        access.bind("default", "finance_analyst")


def test_unbinding_forgets_the_role_and_prints_the_revoke():
    adapter = Adapter()
    access = service(adapter)
    access.create_group("finance")
    access.bind("finance", "finance_analyst")
    assert access.unbind("finance") == {
        "group": "finance",
        "bound_role": "finance_analyst",
        "statement": "REVOKE finance_analyst FROM finance",
    }
    assert access.state.get("bindings", "finance") is None
    with pytest.raises(ValueError, match="no bound role"):
        access.unbind("finance")


def test_a_confirmed_binding_whose_grant_is_gone_is_listed_in_access_health():
    adapter = Adapter()
    access = service(adapter)
    access.create_group("finance")
    access.bind("finance", "finance_analyst")
    adapter.granted["finance"] = {"finance_analyst"}
    access.confirm_binding("finance")
    access.reconcile()
    assert access.health()["missing_bindings"] == []
    adapter.granted["finance"] = set()
    result = access.reconcile()
    assert access.health()["missing_bindings"] == [
        {"group": "finance", "role": "finance", "bound_role": "finance_analyst"}
    ]
    assert result["complete"]


def test_a_pending_binding_is_not_reported_missing():
    access = service()
    access.create_group("finance")
    access.bind("finance", "finance_analyst")
    access.reconcile()
    assert access.health()["missing_bindings"] == []


def test_deleting_a_group_drops_its_own_role_and_leaves_the_bound_one():
    adapter = Adapter(existing={"finance_analyst"})
    access = service(adapter)
    access.create_group("finance")
    access.bind("finance", "finance_analyst")
    adapter.granted["finance"] = {"finance_analyst"}
    access.confirm_binding("finance")
    access.delete_group("finance")
    assert ("drop", "finance") in adapter.ops
    assert not [op for op in adapter.ops if "finance_analyst" in op and op[0] != "holds"]
    assert "finance_analyst" in adapter.roles
    assert access.state.get("bindings", "finance") is None
