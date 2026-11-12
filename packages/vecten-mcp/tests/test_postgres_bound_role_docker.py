"""On the laptop's Postgres, a group binds a role the administrator already has: its members
read through it, the provisioner holds no membership in it, its loss shows in access health,
and deleting the group leaves it.

One container and one server for the module; the tests run in file order.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "bound_served", Path(__file__).with_name("served.py")
)
served = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = served
_spec.loader.exec_module(served)

pytestmark = [pytest.mark.docker, pytest.mark.xdist_group("mcp-postgres-bound-role-docker")]

MODEL = {"version": "0.1.1", "semantic_model": [{"name": "shop", "datasets": []}]}
CLIENT_ROLE = """
CREATE ROLE finance_analyst NOLOGIN;
CREATE SCHEMA ledger;
CREATE TABLE ledger.entries (id int);
GRANT USAGE ON SCHEMA ledger TO finance_analyst;
GRANT SELECT ON ledger.entries TO finance_analyst;
"""


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    from vecten_shared.config import VectenConfig

    if not served.docker_util.docker_available():
        pytest.skip("Docker unavailable")
    root = tmp_path_factory.mktemp("bound-role")
    (root / "osi").mkdir()
    (root / "osi" / "shop.json").write_text(json.dumps(MODEL))
    config = VectenConfig.model_validate(
        {
            "project": {"name": "shop", "scaffold_version": "0.1.0"},
            "users": 5,
            "components": ["platform", "mcp"],
            "mcp": {"admins": ["ada@example.com"]},
        }
    )
    with (
        served.laptop_postgres(root) as pg,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        pg.sql(CLIENT_ROLE)
        with served.serve(root, config, pg, monkeypatch) as stack:
            stack.ada = stack.sign_in("ada@example.com")["access_token"]
            yield stack


def reads_ledger(stack, role):
    return stack.pg.sql(
        "SELECT has_table_privilege(%s, 'ledger.entries', 'SELECT')", params=(role,)
    )[0][0]


def test_a_group_named_like_the_existing_role_is_refused_naming_both_ways_out(stack):
    refused = stack.admin(stack.ada, "groups/create", {"name": "finance_analyst"}, status=400)
    assert "--role" in refused["error"] and "vecten group bind" in refused["error"]


def test_a_member_reads_through_the_bound_role_once_it_is_confirmed(stack):
    stack.admin(stack.ada, "groups/create", {"name": "finance", "role": "fin_readers"})
    bound = stack.admin(stack.ada, "groups/bind", {"group": "finance", "role": "finance_analyst"})[
        "data"
    ]
    assert bound["statement"] == 'GRANT "finance_analyst" TO "fin_readers" WITH INHERIT TRUE;'
    stack.admin(stack.ada, "groups/confirm", {"group": "finance"}, status=400)
    stack.pg.sql(bound["statement"])
    assert stack.admin(stack.ada, "groups/confirm", {"group": "finance"})["data"]["confirmed"]
    jane = stack.admin(
        stack.ada, "users/create", {"email": "jane@example.com", "name": "Jane Doe"}
    )["data"]
    assert not reads_ledger(stack, jane["warehouse_name"])
    stack.admin(stack.ada, "groups/member", {"group": "finance", "id": jane["id"], "member": True})
    assert reads_ledger(stack, jane["warehouse_name"])
    assert not reads_ledger(stack, "analyst")


def test_the_provisioner_holds_no_membership_in_the_bound_role(stack):
    direct = stack.pg.sql(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid "
        "JOIN pg_roles u ON u.oid=m.member "
        "WHERE r.rolname='finance_analyst' AND u.rolname='provisioner'"
    )[0][0]
    assert direct == 0
    for privilege in ("USAGE", "SET"):
        assert not stack.pg.sql(
            "SELECT pg_has_role('provisioner', 'finance_analyst', %s)", params=(privilege,)
        )[0][0]
    assert not reads_ledger(stack, "provisioner")
    runtime = stack.runtime
    runtime.adapter.check_scope(set(dict(runtime.state.scan("provisioner_registry"))))


def test_a_binding_revoked_outside_the_server_is_listed_in_access_health(stack):
    stack.pg.sql('REVOKE "finance_analyst" FROM "fin_readers"')
    stack.admin(stack.ada, "reconcile", {})
    health = stack.admin(stack.ada, "access-health")["data"]
    assert health["missing_bindings"] == [
        {"group": "finance", "role": "fin_readers", "bound_role": "finance_analyst"}
    ]


def test_deleting_the_group_drops_its_role_and_leaves_the_bound_one(stack):
    stack.pg.sql('GRANT "finance_analyst" TO "fin_readers" WITH INHERIT TRUE')
    stack.admin(stack.ada, "groups/delete", {"name": "finance"})
    roles = {row[0] for row in stack.pg.sql("SELECT rolname FROM pg_roles")}
    assert "fin_readers" not in roles and "finance_analyst" in roles
    assert reads_ledger(stack, "finance_analyst")
