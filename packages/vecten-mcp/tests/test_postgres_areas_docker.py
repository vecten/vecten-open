"""On the laptop's Postgres, a restricted area is closed until granted, and once granted to a
group a mart dbt rebuilds in it is readable by that group and by no one else, before any
reconciliation runs.

One container and one server for the module; the tests run in file order.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "areas_served", Path(__file__).with_name("served.py")
)
served = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = served
_spec.loader.exec_module(served)

# The module's tests share one Postgres container and run in order against it, so a CI shard
# takes the module whole.
pytestmark = [pytest.mark.docker, pytest.mark.xdist_group("mcp-postgres-areas-docker")]

SEED = """
CREATE TABLE core_finance.invoice (invoice_id int PRIMARY KEY, amount numeric);
INSERT INTO core_finance.invoice VALUES (1, 100);
CREATE TABLE marts_finance.finance_revenue AS SELECT sum(amount) AS revenue
FROM core_finance.invoice;
"""
MODEL = {"version": "0.1.1", "semantic_model": [{"name": "shop", "datasets": []}]}
# dbt-postgres's table materialization: build beside, swap by renames, drop the old relation.
REBUILD = """
CREATE TABLE marts_finance.finance_revenue__dbt_tmp AS SELECT sum(amount) * 2 AS revenue
FROM core_finance.invoice;
ALTER TABLE marts_finance.finance_revenue RENAME TO finance_revenue__dbt_backup;
ALTER TABLE marts_finance.finance_revenue__dbt_tmp RENAME TO finance_revenue;
DROP TABLE marts_finance.finance_revenue__dbt_backup;
"""
RELATIONS = ("core_finance.invoice", "marts_finance.finance_revenue")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    from vecten_shared.config import VectenConfig

    if not served.docker_util.docker_available():
        pytest.skip("Docker unavailable")
    root = tmp_path_factory.mktemp("areas")
    (root / "osi").mkdir()
    (root / "osi" / "shop.json").write_text(json.dumps(MODEL))
    config = VectenConfig.model_validate(
        {
            "project": {"name": "shop", "scaffold_version": "0.1.0"},
            "users": 5,
            "components": ["platform", "mcp"],
            "mcp": {"admins": ["ada@example.com"]},
            "areas": {"finance": {"access": {"groups": ["finance"]}}},
        }
    )
    with (
        served.laptop_postgres(root, {"VECTEN_AREAS": "finance"}) as pg,
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        pg.sql(SEED, user="loader")
        with served.serve(root, config, pg, monkeypatch) as stack:
            stack.ada = stack.sign_in("ada@example.com")["access_token"]
            stack.admin(stack.ada, "groups/create", {"name": "finance"})
            yield stack


def reads(stack, role):
    """The area relations `role` may select from, and the area schemas it may use."""
    schemas = [
        schema
        for schema in ("core_finance", "marts_finance")
        if stack.pg.sql("SELECT has_schema_privilege(%s, %s, 'USAGE')", params=(role, schema))[0][0]
    ]
    tables = [
        name
        for name in RELATIONS
        if stack.pg.sql("SELECT has_table_privilege(%s, %s, 'SELECT')", params=(role, name))[0][0]
    ]
    return schemas, tables


def test_the_setup_created_the_area_as_loader_and_no_role_reads_it(stack):
    owners = stack.pg.sql(
        "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace "
        "WHERE nspname IN ('core_finance', 'marts_finance') ORDER BY 1"
    )
    assert owners == [("core_finance", "loader"), ("marts_finance", "loader")]
    stack.admin(stack.ada, "reconcile", {})
    for role in ("analyst", "finance"):
        assert reads(stack, role) == ([], []), role


def test_an_area_grant_opens_both_schemas_to_the_group_only(stack):
    granted = stack.admin(
        stack.ada, "grant", {"group": "finance", "kind": "area", "target": "finance"}
    )
    assert granted["ok"], granted
    assert reads(stack, "finance") == (["core_finance", "marts_finance"], list(RELATIONS))
    assert reads(stack, "analyst") == ([], [])


def test_a_rebuilt_mart_is_readable_by_the_group_and_refused_for_default_before_reconciling(
    stack,
):
    stack.pg.sql(REBUILD, user="loader")
    stack.pg.sql("CREATE TABLE marts_finance.finance_margin (margin numeric)", user="loader")
    assert reads(stack, "finance")[1] == list(RELATIONS)
    assert stack.pg.sql(
        "SELECT has_table_privilege('finance', 'marts_finance.finance_margin', 'SELECT')"
    )[0][0]
    assert reads(stack, "analyst") == ([], [])
    assert not stack.pg.sql(
        "SELECT has_table_privilege('analyst', 'marts_finance.finance_margin', 'SELECT')"
    )[0][0]


def test_a_hand_grant_in_the_area_is_revoked_and_the_group_keeps_it(stack):
    stack.pg.sql(
        "GRANT USAGE ON SCHEMA marts_finance TO analyst; "
        "GRANT SELECT ON marts_finance.finance_revenue TO analyst"
    )
    stack.admin(stack.ada, "reconcile", {})
    health = stack.admin(stack.ada, "access-health")["data"]
    assert reads(stack, "analyst") == ([], [])
    assert {(r["group"], r["schema"]) for r in health["revoked"]} == {("default", "marts_finance")}
    assert reads(stack, "finance") == (["core_finance", "marts_finance"], list(RELATIONS))


def test_revoking_the_area_closes_both_schemas_and_their_future_tables(stack):
    stack.admin(stack.ada, "revoke", {"group": "finance", "kind": "area", "target": "finance"})
    assert reads(stack, "finance") == ([], [])
    stack.pg.sql("CREATE TABLE core_finance.payment (id int)", user="loader")
    assert not stack.pg.sql(
        "SELECT has_table_privilege('finance', 'core_finance.payment', 'SELECT')"
    )[0][0]
