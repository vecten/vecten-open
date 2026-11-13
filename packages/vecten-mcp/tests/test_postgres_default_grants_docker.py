"""On the laptop's Postgres, the MCP's state alone owns `analyst`'s reads: a revoke of `marts`
from the default group survives the next ship, and reconciliation revokes hand-made read
grants in the convention schemas while leaving every other schema alone. A dataset grant that
a dbt rebuild drops comes back as soon as the dbt DAG records the build.

One container and one server for the module; the tests run in file order.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "default_grants_served", Path(__file__).with_name("served.py")
)
served = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = served
_spec.loader.exec_module(served)

# The module's tests share one Postgres container and run in order against it, so a CI shard
# takes the module whole.
pytestmark = [pytest.mark.docker, pytest.mark.xdist_group("mcp-postgres-default-grants-docker")]

SEED = """
CREATE TABLE core.customers (customer_id int PRIMARY KEY);
CREATE TABLE marts.orders (order_id int PRIMARY KEY, amount numeric);
INSERT INTO marts.orders VALUES (1, 100);
"""
MODEL = {
    "version": "0.1.1",
    "semantic_model": [
        {
            "name": "shop",
            "datasets": [
                {
                    "name": "orders",
                    "source": "warehouse.marts.orders",
                    "primary_key": ["order_id"],
                    "fields": [
                        {
                            "name": name,
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": name}]
                            },
                        }
                        for name in ("order_id", "amount")
                    ],
                }
            ],
        }
    ],
}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    from vecten_shared.config import VectenConfig
    from vecten_shared.osi.revision import content_revision

    if not served.docker_util.docker_available():
        pytest.skip("Docker unavailable")
    root = tmp_path_factory.mktemp("default-grants")
    (root / "osi").mkdir()
    (root / "osi" / "shop.json").write_text(json.dumps(MODEL))
    artifacts = root / "artifacts" / "dbt"
    artifacts.mkdir(parents=True)
    (artifacts / "built_commit").write_text(content_revision(root / "osi") + "\n")
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
        pg.sql(SEED, user="loader")
        environ = {"VECTEN_ARTIFACT_DIR": str(root / "artifacts")}
        with served.serve(root, config, pg, monkeypatch, environ=environ) as stack:
            stack.ada = stack.sign_in("ada@example.com")["access_token"]
            stack.root = root
            yield stack


def analyst_reads(stack, schema):
    """What `analyst` holds in `schema`: USAGE, default SELECT, and the tables it selects."""
    usage = stack.pg.sql("SELECT has_schema_privilege('analyst', %s, 'USAGE')", params=(schema,))[
        0
    ][0]
    defaults = stack.pg.sql(
        "SELECT count(*) FROM pg_default_acl d JOIN pg_namespace n ON n.oid=d.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
        "WHERE n.nspname=%s AND a.grantee='analyst'::regrole",
        params=(schema,),
    )[0][0]
    tables = stack.pg.sql(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(c.relacl) a "
        "WHERE n.nspname=%s AND a.grantee='analyst'::regrole AND a.privilege_type='SELECT'",
        params=(schema,),
    )
    return {"usage": usage, "defaults": defaults, "tables": sorted(row[0] for row in tables)}


def reconcile(stack):
    stack.admin(stack.ada, "reconcile", {})
    return stack.admin(stack.ada, "access-health")["data"]


def test_the_first_reconciliation_grants_the_default_group_core_and_marts(stack):
    reconcile(stack)
    for schema in ("core", "marts"):
        reads = analyst_reads(stack, schema)
        assert reads["usage"] and reads["defaults"], (schema, reads)
    assert analyst_reads(stack, "marts")["tables"] == ["orders"]


def test_a_revoked_marts_survives_the_next_ship(stack):
    stack.admin(stack.ada, "revoke", {"group": "default", "kind": "schema", "target": "marts"})
    assert analyst_reads(stack, "marts") == {"usage": False, "defaults": 0, "tables": []}
    stack.pg.rerun_setup()
    health = reconcile(stack)
    assert analyst_reads(stack, "marts") == {"usage": False, "defaults": 0, "tables": []}
    assert health["revoked"] == [] and health["failures"] == []
    stack.pg.sql("CREATE TABLE marts.rebuilt (id int)", user="loader")
    assert analyst_reads(stack, "marts")["tables"] == []
    assert analyst_reads(stack, "core")["usage"]


def test_a_hand_grant_in_marts_is_revoked_and_one_in_another_schema_stays(stack):
    stack.pg.sql(
        "GRANT USAGE ON SCHEMA marts TO analyst; GRANT SELECT ON marts.orders TO analyst; "
        "ALTER DEFAULT PRIVILEGES FOR ROLE loader IN SCHEMA marts "
        "GRANT SELECT ON TABLES TO analyst"
    )
    stack.pg.sql(
        "CREATE SCHEMA reference; CREATE TABLE reference.countries (code text); "
        "GRANT USAGE ON SCHEMA reference TO analyst; "
        "GRANT SELECT ON reference.countries TO analyst"
    )
    health = reconcile(stack)
    assert analyst_reads(stack, "marts") == {"usage": False, "defaults": 0, "tables": []}
    revoked = {(r["group"], r["privilege"], r["schema"], r["relation"]) for r in health["revoked"]}
    assert revoked == {
        ("default", "USAGE", "marts", None),
        ("default", "SELECT", "marts", "orders"),
        ("default", "FUTURE SELECT", "marts", None),
    }
    assert analyst_reads(stack, "reference") == {
        "usage": True,
        "defaults": 0,
        "tables": ["countries"],
    }


def test_report_mode_lists_a_hand_grant_and_keeps_it(stack):
    stack.admin(stack.ada, "settings/set", {"key": "reconcile_drift", "value": "report"})
    stack.pg.sql("GRANT SELECT ON marts.orders TO analyst")
    health = reconcile(stack)
    assert analyst_reads(stack, "marts")["tables"] == ["orders"]
    assert [(d["privilege"], d["relation"]) for d in health["drift"]] == [("SELECT", "orders")]
    stack.admin(stack.ada, "settings/set", {"key": "reconcile_drift", "value": "revoke"})
    reconcile(stack)
    assert analyst_reads(stack, "marts")["tables"] == []


# dbt-postgres's table materialization: build beside, swap by renames, drop the old relation.
REBUILD = """
CREATE TABLE marts.orders__dbt_tmp AS SELECT * FROM marts.orders;
ALTER TABLE marts.orders RENAME TO orders__dbt_backup;
ALTER TABLE marts.orders__dbt_tmp RENAME TO orders;
DROP TABLE marts.orders__dbt_backup;
"""


def sales_reads_orders(stack):
    return stack.pg.sql("SELECT has_table_privilege('sales', 'marts.orders', 'SELECT')")[0][0]


def test_a_dataset_grant_dropped_by_a_rebuild_returns_once_the_build_is_recorded(stack):
    from vecten_platform.dbt.artifacts import BUILD_FINISHED_KEY, DirectorySink

    stack.admin(stack.ada, "groups/create", {"name": "sales"})
    stack.admin(stack.ada, "grant", {"group": "sales", "kind": "dataset", "target": "orders"})
    reconcile(stack)
    assert sales_reads_orders(stack)
    stack.pg.sql(REBUILD, user="loader")
    assert not sales_reads_orders(stack)
    runtime = stack.runtime
    # The fifteen-minute schedule is not due, so only the recorded build can reconcile.
    runtime.last_reconcile = runtime.oauth.clock()
    runtime.tick()
    assert not sales_reads_orders(stack)
    sink = DirectorySink(Path(stack.root) / "artifacts")
    sink.put_text(BUILD_FINISHED_KEY, "2026-11-12T10:00:00+00:00\n")
    runtime.tick()
    assert sales_reads_orders(stack)
    assert analyst_reads(stack, "marts")["tables"] == []
