"""A group's role can be named at creation, and a group can bind a role the client already has,
which the server prints the statement for, records once it is in place, and never manages."""

import importlib.util
import sys
from pathlib import Path

import pytest

from vecten_mcp.access import AccessService
from vecten_mcp.state import StateStore
from vecten_mcp.warehouse.base import RoleExists
from vecten_shared.identity_setup import ACCESS_VERSION

_spec = importlib.util.spec_from_file_location(
    "groups_warehouse_fakes", Path(__file__).with_name("warehouse_fakes.py")
)
fakes = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fakes
_spec.loader.exec_module(fakes)


class Adapter:
    """Roles that exist, and the roles granted to each group role."""

    def __init__(self, existing=()):
        self.roles, self.granted, self.ops = set(existing), {}, []
        self.problems = {}

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

    def granted_roles(self, role):
        return set(self.granted.get(role, set()))

    def bound_role_problem(self, bound, managed):
        self.ops.append(("check", bound, frozenset(managed)))
        return self.problems.get(bound)

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
    assert not [
        op for op in adapter.ops if "finance_analyst" in op and op[0] not in {"holds", "check"}
    ]
    assert "finance_analyst" in adapter.roles
    assert access.state.get("bindings", "finance") is None


def test_binding_the_same_role_again_prints_the_statement_and_keeps_the_record():
    adapter = Adapter()
    access = service(adapter)
    access.create_group("finance")
    access.bind("finance", "finance_analyst")
    adapter.granted["finance"] = {"finance_analyst"}
    access.confirm_binding("finance")
    adapter.granted["finance"] = set()
    access.reconcile()
    assert access.health()["missing_bindings"]
    again = access.bind("finance", "finance_analyst")
    assert again["confirmed"] is True and again["statement"] == "GRANT finance_analyst TO finance"
    access.reconcile()
    assert access.health()["missing_bindings"] == [
        {"group": "finance", "role": "finance", "bound_role": "finance_analyst"}
    ]


def test_a_role_another_group_binds_cannot_become_a_group_role():
    access = service()
    access.create_group("finance")
    access.bind("finance", "fin_readers")
    with pytest.raises(ValueError, match="bound to group finance"):
        access.create_group("board", role="fin_readers")


def test_bind_and_confirm_refuse_a_role_the_warehouse_reports_too_powerful():
    adapter = Adapter()
    access = service(adapter)
    access.create_group("finance")
    adapter.problems["wrapper"] = "wrapper is a member of pg_read_all_data"
    with pytest.raises(ValueError, match="pg_read_all_data"):
        access.bind("finance", "wrapper")
    assert access.state.get("bindings", "finance") is None
    access.bind("finance", "finance_analyst")
    adapter.granted["finance"] = {"finance_analyst"}
    adapter.problems["finance_analyst"] = "finance_analyst is SUPERUSER"
    with pytest.raises(ValueError, match="SUPERUSER"):
        access.confirm_binding("finance")
    assert access.state.get("bindings", "finance")["confirmed"] is False
    managed = [op[2] for op in adapter.ops if op[0] == "check"]
    assert managed and all({"analyst", "finance", "provisioner"} <= m for m in managed)


def test_a_role_nested_under_a_group_role_other_than_its_binding_is_listed():
    adapter = Adapter()
    access = service(adapter)
    access.create_group("finance")
    access.bind("finance", "finance_analyst")
    adapter.granted["finance"] = {"finance_analyst", "SYSADMIN"}
    access.confirm_binding("finance")
    result = access.reconcile()
    assert access.health()["unbound_roles"] == [
        {"group": "finance", "role": "finance", "granted_role": "SYSADMIN"}
    ]
    assert result["complete"]
    access.unbind("finance")
    access.reconcile()
    assert {r["granted_role"] for r in access.health()["unbound_roles"]} == {
        "SYSADMIN",
        "finance_analyst",
    }
    adapter.granted["finance"] = set()
    access.reconcile()
    assert access.health()["unbound_roles"] == []


def test_deleting_a_group_whose_revoke_fails_leaves_no_applied_record():
    class Refusing(Adapter):
        def schema_grant(self, role, schema, grant=True):
            if not grant:
                raise RuntimeError("refused")
            super().schema_grant(role, schema, grant)

    access = service(Refusing())
    access.create_group("finance")
    access.grant("finance", "schema", "marts")
    access.delete_group("finance")
    assert not [r for _, r in access.state.scan("applied_grants") if r["group"] == "finance"]
    assert access.reconcile()["complete"]


# A shared Snowflake account: project acme's environment dev, whose names end in _ACME_DEV.


def snowflake_service(*, shared_account):
    adapter, cursor = fakes.snowflake([], shared_account=shared_account)
    access = service(adapter)
    access.create_group("finance")
    access.create_group("marketing")
    return access, cursor


ANOTHER_ENVIRONMENTS = ["ANALYST_ACME_PROD", "loader_acme_staging", "Finance_Acme_Prod"]
OWN_RESERVED = [
    f"{name}_ACME_DEV" for name in ("analyst", "loader", "provisioner", "reader", "mcp_state")
]


@pytest.mark.parametrize(
    "bound",
    [*ANOTHER_ENVIRONMENTS, *OWN_RESERVED, "loader_acme_dev", "MARKETING_ACME_DEV"],
)
def test_a_shared_account_refuses_binding_a_role_of_any_environment_of_the_project(bound):
    access, cursor = snowflake_service(shared_account=True)
    before = list(cursor.statements)
    with pytest.raises(ValueError, match="cannot be bound"):
        access.bind("finance", bound)
    assert access.state.get("bindings", "finance") is None
    # The refusal comes from the name alone; nothing ran against the warehouse for it.
    assert cursor.statements == before


def test_a_shared_account_binds_a_client_role():
    access, _ = snowflake_service(shared_account=True)
    assert access.bind("finance", "FINANCE_ANALYST")["confirmed"] is False
    assert access.bind("marketing", "ACME_DEV_READERS")["bound_role"] == "ACME_DEV_READERS"


@pytest.mark.parametrize("role", ["analyst_acme_prod", "team_acme_dev"])
def test_a_shared_account_refuses_a_group_role_named_with_an_environments_suffix(role):
    access, cursor = snowflake_service(shared_account=True)
    with pytest.raises(ValueError, match="_ACME_"):
        access.create_group("team", role=role)
    assert access.state.group("team") is None
    assert not any(role.upper() in sql for sql, _ in cursor.statements)


@pytest.mark.parametrize("bound", [*ANOTHER_ENVIRONMENTS, *OWN_RESERVED, "MARKETING_ACME_DEV"])
def test_the_fixed_layout_binds_names_that_carry_no_meaning_in_its_account(bound):
    access, cursor = snowflake_service(shared_account=False)
    assert access.bind("finance", bound)["statement"] == (
        f'GRANT ROLE "{bound.upper()}" TO ROLE "FINANCE";'
    )
    access.create_group("team", role="team_acme_prod")
    assert ('CREATE ROLE "TEAM_ACME_PROD"', None) in cursor.statements


@pytest.mark.parametrize("bound", ["loader", "ANALYST", "Marketing", "provisioner"])
def test_the_fixed_layout_still_refuses_its_own_managed_roles(bound):
    access, _ = snowflake_service(shared_account=False)
    with pytest.raises(ValueError, match="manages"):
        access.bind("finance", bound)
