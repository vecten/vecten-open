"""Which bound groups an app admits, recomputed whenever grants, the model or the bundle change."""

import asyncio
import json

from vecten_mcp.access import AccessService
from vecten_mcp.apps import ADMISSION, AppScopes, BoundApp, read_bound_apps
from vecten_mcp.identity import Identity
from vecten_mcp.model import ModelService
from vecten_mcp.runtime import Runtime
from vecten_mcp.state import StateStore
from vecten_shared.apps import parse_app
from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import RECORD_FILE, Extension, record_text

FINANCE = BoundApp(
    extension="acme-sales",
    name="finance",
    title="Finance",
    kind="container",
    datasets=("revenue",),
    groups=("finance", "default"),
)
OVERVIEW = BoundApp(
    extension="acme-sales",
    name="overview",
    title="Sales overview",
    kind="container",
    datasets=("orders",),
    groups=("default",),
)
SOURCES = {"revenue": "marts_finance.revenue", "orders": "marts.orders"}


class Adapter:
    def __init__(self):
        self.fail = False

    def create_group(self, role):
        pass

    def membership(self, role, name, grant=True):
        pass

    def dataset_grant(self, role, source, grant=True):
        if self.fail:
            raise RuntimeError("warehouse refused the grant")

    def schema_grant(self, role, schema, grant=True):
        pass


def setup(*apps, kind="postgres", database=None):
    state = StateStore("sqlite://")
    adapter = Adapter()
    access = AccessService(state, adapter)
    access.seed_defaults()
    access.create_group("finance")
    scopes = AppScopes(state, {app.name: app for app in apps}, kind=kind, database=database)
    access.apps = scopes
    return state, adapter, access, scopes


def test_a_group_that_cannot_read_a_restricted_dataset_is_not_admitted():
    state, _, access, scopes = setup(FINANCE)
    access.model_changed(SOURCES)
    access.grant("finance", "dataset", "revenue")
    assert scopes.admitted("finance") == {"finance"}
    [entry] = access.health()["apps"]
    assert entry == {
        "app": "acme-sales.finance",
        "group": "default",
        "dataset": "revenue",
        "source": "marts_finance.revenue",
        "message": "group default may open acme-sales.finance but cannot read "
        "marts_finance.revenue",
    }


def test_a_grant_added_later_admits_the_group_and_clears_the_entry():
    state, _, access, scopes = setup(FINANCE)
    access.model_changed(SOURCES)
    access.grant("finance", "dataset", "revenue")
    access.grant("default", "dataset", "revenue")
    access.reconcile()
    assert scopes.admitted("finance") == {"finance", "default"}
    assert access.health()["apps"] == []
    access.revoke("default", "dataset", "revenue")
    assert scopes.admitted("finance") == {"finance"}
    assert [entry["group"] for entry in access.health()["apps"]] == ["default"]


def test_a_schema_grant_admits_the_group_for_a_dataset_in_that_schema():
    _, _, access, scopes = setup(OVERVIEW)
    access.model_changed(SOURCES)
    assert scopes.admitted("overview") == {"default"}
    access.revoke("default", "schema", "marts")
    assert scopes.admitted("overview") == frozenset()


def test_a_schema_grant_covers_a_three_part_source_only_in_the_projects_database():
    app = BoundApp("acme-sales", "overview", "", "container", ("orders",), ("default",))
    _, _, access, scopes = setup(app, kind="snowflake", database="ANALYTICS")
    access.model_changed({"orders": "analytics.marts.orders"})
    assert scopes.admitted("overview") == {"default"}
    access.model_changed({"orders": "other.marts.orders"})
    assert scopes.admitted("overview") == frozenset()


def test_a_desired_grant_the_warehouse_did_not_apply_admits_no_one():
    state, adapter, access, scopes = setup(FINANCE)
    access.model_changed(SOURCES)
    adapter.fail = True
    state.put(
        "grants",
        access._key("finance", "dataset", "revenue"),
        {"group": "finance", "kind": "dataset", "target": "revenue"},
    )
    assert not access.reconcile()["complete"]
    assert scopes.admitted("finance") == frozenset()


def test_a_bound_group_that_does_not_exist_is_listed():
    app = BoundApp("acme-sales", "overview", "", "container", ("orders",), ("sales", "default"))
    _, _, access, scopes = setup(app)
    access.model_changed(SOURCES)
    assert scopes.admitted("overview") == {"default"}
    assert [(e["group"], e.get("reason")) for e in access.health()["apps"]] == [
        ("sales", "no such group")
    ]
    access.create_group("sales")
    assert [(e["group"], e.get("dataset")) for e in access.health()["apps"]] == [
        ("sales", "orders")
    ]


def test_a_dataset_the_model_lacks_admits_no_group_and_is_a_model_warning():
    # The grants cannot be checked against a source the model does not name, so no group is
    # admitted until it does.
    app = BoundApp("acme-sales", "overview", "", "container", ("orders", "gone"), ("default",))
    _, _, access, scopes = setup(app)
    access.model_changed(SOURCES)
    assert scopes.admitted("overview") == frozenset()
    [entry] = access.health()["apps"]
    assert (entry["group"], entry["dataset"], entry["reason"]) == (
        "default",
        "gone",
        "not in the model",
    )
    access.model_changed({**SOURCES, "gone": "marts.gone"})
    assert scopes.admitted("overview") == {"default"}
    model = type("Model", (), {"datasets": {"orders": {}}})()
    assert [(w["code"], w["app"], w["dataset"]) for w in scopes.missing(model)] == [
        ("app_dataset_missing", "acme-sales.overview", "gone")
    ]


def test_model_health_lists_a_declared_dataset_the_model_lacks(tmp_path):
    state = StateStore("sqlite://")
    osi = tmp_path / "osi"
    osi.mkdir()
    field = {"dialects": [{"dialect": "ANSI_SQL", "expression": "amount"}]}
    (osi / "datasets.json").write_text(
        json.dumps(
            {
                "version": "0.1.1",
                "semantic_model": [
                    {
                        "name": "sales",
                        "datasets": [
                            {
                                "name": "orders",
                                "source": "marts.orders",
                                "fields": [{"name": "amount", "expression": field}],
                            }
                        ],
                    }
                ],
            }
        )
    )
    app = BoundApp("acme-sales", "overview", "", "container", ("orders", "gone"), ("default",))
    scopes = AppScopes(state, {"overview": app}, kind="postgres")
    model = ModelService(
        osi, state, kind="postgres", source_exists=lambda table: True, checks=scopes.missing
    )
    model.reload()
    health = model.health()
    assert health["status"] == "ready"
    assert [(w["code"], w["dataset"]) for w in health["warnings"]] == [
        ("app_dataset_missing", "gone")
    ]
    unscoped = ModelService(osi, state, kind="postgres", source_exists=lambda table: True)
    unscoped.reload()
    assert unscoped.health()["warnings"] == []


def test_the_admission_of_an_app_no_longer_bound_is_dropped_and_groups_follow_the_bundle():
    state, _, access, scopes = setup(FINANCE, OVERVIEW)
    access.model_changed(SOURCES)
    assert state.get(ADMISSION, "acme-sales.finance")
    shipped = BoundApp("acme-sales", "overview", "", "container", ("orders",), ("finance",))
    restarted = AppScopes(state, {"overview": shipped}, kind="postgres")
    # Until the new bundle's admission is computed, only groups it binds can be admitted.
    assert restarted.admitted("overview") == frozenset()
    restarted.admit(access.datasets)
    assert state.get(ADMISSION, "acme-sales.finance") is None
    assert restarted.admitted("overview") == frozenset()
    assert restarted.health()[0]["group"] == "finance"


def test_the_server_computes_admission_when_it_starts():
    state, _, access, _ = setup()
    access.model_changed(SOURCES)
    scopes = AppScopes(state, {"overview": OVERVIEW}, kind="postgres")
    runtime = Runtime(state, None, None, None, None, access=access)
    runtime.apps = scopes

    async def start():
        runtime.start()
        runtime.maintenance_task.cancel()

    asyncio.run(start())
    assert scopes.admitted("overview") == {"default"}


def test_access_health_without_bound_apps_keeps_its_shape():
    _, _, access, _ = setup()
    access.model_changed(SOURCES)
    assert set(access.health()) == {"checked_at", "failures"}


def test_only_a_service_principal_is_marked_as_an_app_principal():
    state, _, _, scopes = setup(OVERVIEW)
    person = state.create_principal(
        Identity("password", "ada@acme.com", "ada@acme.com", "Ada"), "ada", provisioned=True
    )
    try:
        scopes.mark(person["id"], "acme-sales", "overview")
    except ValueError:
        pass
    else:
        raise AssertionError("a person was marked as an app principal")
    assert scopes.scope(person["id"]) is None


def test_bound_apps_come_from_the_bundles_vecten_toml_and_record(tmp_path):
    declared = {
        "title": "Sales overview",
        "description": "",
        "kind": "container",
        "runtime": "streamlit",
        "entry": "acme_sales/app.py",
        "datasets": ["orders", "customers"],
        "groups": ["default"],
    }
    record = {
        "acme-sales": Extension(
            "1.0", (), apps={"overview": parse_app("acme-sales", "overview", declared).app}
        )
    }
    (tmp_path / RECORD_FILE).write_text(record_text(record))
    config = VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "extensions": ["acme-sales"],
            "apps": {
                "acme-sales": {
                    "overview": {"groups": ["sales", "finance"]},
                    "retired": {"groups": ["default"]},
                }
            },
        }
    )
    apps = read_bound_apps(tmp_path, config)
    assert apps["overview"] == BoundApp(
        extension="acme-sales",
        name="overview",
        title="Sales overview",
        kind="container",
        datasets=("orders", "customers"),
        groups=("sales", "finance"),
    )
    # A binding the record does not declare reads nothing.
    assert apps["retired"].datasets == ()
