"""`vecten app connect`: the app principal's credentials land in the scope of the app's host,
locally in `.env`, and are never printed."""

import json

import pytest
from dotenv import dotenv_values

from vecten.next.gitstate import GitState
from vecten.next.items import Item
from vecten.next.post_deploy import post_deploy_items
from vecten.secrets import recorded_secret_names
from vecten_shared import provider as provider_module
from vecten_shared.apps import parse_app
from vecten_shared.config import load_config, save_config
from vecten_shared.extension_record import RECORD_FILE, Extension, record_text
from vecten_shared.output import ExitCode, Failure

from .fixture_project import render_fixture
from .test_next_deploy import signed_in

SECRET = "the-client-secret-value"
OVERVIEW = {
    "title": "Sales overview",
    "description": "",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme_sales/app.py",
    "datasets": ["orders"],
    "groups": ["default"],
}
WAREHOUSE = {
    "title": "Warehouse",
    "description": "",
    "kind": "snowflake",
    "entry": "acme_sales/warehouse.py",
    "datasets": ["orders"],
    "groups": ["default"],
}
STORE: dict[str, str] = {}
CLEAN = GitState(has_repo=True, has_origin=True, dirty=False, ahead=False)


class FakeCloud:
    def __init__(self, project_root, env):
        self.env = env

    def put_secret(self, name, value):
        STORE[name] = value

    def list_secrets(self):
        return sorted(STORE)


class FakeAdmin:
    calls: list = []
    failure: Failure | None = None
    created = True
    secret = SECRET
    # The shipped apps the admin API lists, or a Failure it raises.
    listing: object = []

    def __init__(self, root, env):
        self.env = env

    def read(self, operation, params):
        assert operation == "apps"
        if isinstance(self.listing, Failure):
            raise self.listing
        return self.listing

    def call(self, operation, data=None):
        if self.failure:
            raise self.failure
        self.calls.append((self.env, operation, data))
        return {
            "app": data["name"],
            "created": self.created,
            "principal": {"id": "p-app", "name": "App", "warehouse_name": "svc_app_overview"},
            "groups": ["sales"],
            "credentials": {"client_id": "the-client-id", "client_secret": self.secret},
        }


@pytest.fixture
def project(tmp_path, monkeypatch):
    STORE.clear()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = render_fixture(tmp_path, "postgres", deploy=True, domain="acme.test")
    record = {
        "acme-sales": Extension(
            "1.2.0",
            (),
            apps={
                "overview": parse_app("acme-sales", "overview", OVERVIEW).app,
                "warehouse": parse_app("acme-sales", "warehouse", WAREHOUSE).app,
            },
        )
    }
    (root / RECORD_FILE).write_text(record_text(record))
    config = load_config(root)
    config = config.model_validate(
        config.model_dump()
        | {
            "extensions": ["acme-sales"],
            "environments": {"prod": {"bootstrapped": True}},
            "apps": {
                "acme-sales": {
                    "overview": {"groups": ["sales"], "hostname": "sales"},
                    "warehouse": {"groups": ["sales"]},
                }
            },
        }
    )
    save_config(root, config)
    monkeypatch.chdir(root)
    monkeypatch.setitem(provider_module._REGISTRY, "aws", FakeCloud)
    return root


@pytest.fixture
def fake_admin(project, monkeypatch):
    monkeypatch.setattr(FakeAdmin, "calls", [])
    monkeypatch.setattr(FakeAdmin, "failure", None)
    monkeypatch.setattr(FakeAdmin, "created", True)
    monkeypatch.setattr(FakeAdmin, "secret", SECRET)
    monkeypatch.setattr(FakeAdmin, "listing", [])
    monkeypatch.setattr("vecten.mcp_client.AdminClient", FakeAdmin)


def connect(invoke, *args):
    return invoke("app", "connect", "acme-sales.overview", *args, "--json")


def test_first_connection_stores_both_names_in_the_platform_scope_and_prints_no_secret(
    invoke, project, fake_admin
):
    result = connect(invoke, "--env", "prod")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert FakeAdmin.calls == [
        ("prod", "apps/connect", {"name": "acme-sales.overview", "adopt": False})
    ]
    assert STORE == {
        "APP_OVERVIEW_CLIENT_ID": "the-client-id",
        "APP_OVERVIEW_CLIENT_SECRET": SECRET,
    }
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    assert doc["secrets"] == ["APP_OVERVIEW_CLIENT_ID", "APP_OVERVIEW_CLIENT_SECRET"]
    assert doc["principal"] == "svc_app_overview" and doc["created"] is True
    assert SECRET not in result.stdout + result.stderr
    assert "the-client-id" not in result.stdout + result.stderr
    assert {"APP_OVERVIEW_CLIENT_ID", "APP_OVERVIEW_CLIENT_SECRET"} <= recorded_secret_names(
        project, "prod"
    )


def test_the_human_output_names_the_secrets_without_their_values(invoke, project, fake_admin):
    result = invoke("app", "connect", "acme-sales.overview", "--env", "prod")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "APP_OVERVIEW_CLIENT_SECRET" in result.stdout
    assert SECRET not in result.stdout + result.stderr


def test_an_app_on_the_mcp_host_stores_its_mcp_names(invoke, project, fake_admin):
    record = json.loads((project / RECORD_FILE).read_text())
    services = record["extensions"]["acme-sales"]["apps"]["overview"]
    services["components"] = ["mcp"]
    (project / RECORD_FILE).write_text(json.dumps(record))
    assert connect(invoke, "--env", "prod").exit_code == 0
    assert set(STORE) == {"MCP_APP_OVERVIEW_CLIENT_ID", "MCP_APP_OVERVIEW_CLIENT_SECRET"}


def test_connecting_again_replaces_the_stored_secret(invoke, project, fake_admin, monkeypatch):
    assert connect(invoke, "--env", "prod").exit_code == 0
    monkeypatch.setattr(FakeAdmin, "created", False)
    monkeypatch.setattr(FakeAdmin, "secret", "rotated-secret")
    result = connect(invoke, "--env", "prod")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["created"] is False
    assert STORE["APP_OVERVIEW_CLIENT_SECRET"] == "rotated-secret"
    assert "rotated-secret" not in result.stdout


def test_local_writes_both_to_dotenv(invoke, project, fake_admin):
    result = connect(invoke, "--env", "local")
    assert result.exit_code == 0, result.stdout + result.stderr
    values = dotenv_values(project / ".env")
    assert values["APP_OVERVIEW_CLIENT_ID"] == "the-client-id"
    assert values["APP_OVERVIEW_CLIENT_SECRET"] == SECRET
    assert STORE == {}
    assert SECRET not in result.stdout
    # No container on the laptop reaches the MCP, which the output says.
    assert "once deployed" in json.loads(result.stdout)["message"]


@pytest.mark.parametrize(
    ("name", "code", "reason"),
    [
        ("acme-sales.missing", ExitCode.REFUSED, "not bound"),
        ("other.overview", ExitCode.REFUSED, "not bound"),
        ("acme-sales.warehouse", ExitCode.REFUSED, "snowflake"),
        ("overview", ExitCode.USAGE, "<extension>.<app>"),
    ],
)
def test_refusals_create_and_store_nothing(invoke, project, fake_admin, name, code, reason):
    result = invoke("app", "connect", name, "--env", "prod", "--json")
    assert result.exit_code == code, result.stdout
    assert reason in json.loads(result.stdout)["error"]
    assert FakeAdmin.calls == [] and STORE == {}


def test_without_a_cached_token_it_names_vecten_login_and_stores_nothing(invoke, project):
    result = connect(invoke, "--env", "prod")
    assert result.exit_code == ExitCode.PREREQUISITE, result.stdout
    assert "vecten login" in json.loads(result.stdout)["error"]
    assert STORE == {}
    assert "APP_OVERVIEW_CLIENT_SECRET" not in recorded_secret_names(project, "prod")


def test_an_environment_without_a_cloud_refuses_before_the_mcp_is_asked(
    invoke, project, fake_admin
):
    result = connect(invoke, "--env", "staging")
    assert result.exit_code != 0
    assert FakeAdmin.calls == []


def test_a_store_failure_after_rotation_says_to_run_it_again(
    invoke, project, fake_admin, monkeypatch
):
    def broken(self, name, value):
        raise RuntimeError(f"cannot write {value}")

    monkeypatch.setattr(FakeCloud, "put_secret", broken)
    result = connect(invoke, "--env", "prod")
    assert result.exit_code == ExitCode.TOOL_FAILED
    error = json.loads(result.stdout)["error"]
    assert "run `vecten app connect acme-sales.overview --env prod` again" in error
    assert SECRET not in result.stdout + result.stderr


def test_a_store_failure_says_what_is_stored_and_that_a_rerun_fixes_it(
    invoke, project, fake_admin, monkeypatch
):
    def secret_fails(self, name, value):
        if name.endswith("_SECRET"):
            raise RuntimeError(f"cannot write {value}")
        STORE[name] = value

    monkeypatch.setattr(FakeCloud, "put_secret", secret_fails)
    result = connect(invoke, "--env", "prod")
    error = json.loads(result.stdout)["error"]
    assert "APP_OVERVIEW_CLIENT_ID is stored" in error
    assert "APP_OVERVIEW_CLIENT_SECRET is not" in error
    assert "the app's previous secret no longer works" in error
    assert "stores both" in error
    assert SECRET not in result.stdout + result.stderr


def test_a_rotation_says_the_running_app_stops_authenticating_until_a_ship(
    invoke, project, fake_admin, monkeypatch
):
    monkeypatch.setattr(FakeAdmin, "created", False)
    message = json.loads(connect(invoke, "--env", "prod").stdout)["message"]
    assert "cannot authenticate until" in message
    assert "vecten ship --env prod" in message


def test_a_local_rotation_names_up(invoke, project, fake_admin, monkeypatch):
    monkeypatch.setattr(FakeAdmin, "created", False)
    message = json.loads(connect(invoke, "--env", "local").stdout)["message"]
    assert "cannot authenticate until" in message and "vecten up" in message


def deployed(project):
    config = load_config(project)
    facts = config.environments["prod"]
    facts.bootstrapped = True
    facts.deployed_commit = "abc123"
    facts.first_user_added = True
    config.mcp.auth = "password"
    config.mcp.admins = ["ada@acme.test"]
    facts.admin_link_issued = True
    save_config(project, config)
    return config


def listed(principal):
    return [{"app": "acme-sales.overview", "kind": "container", "principal": principal}]


MARKED = {"id": "p-app", "warehouse_name": "svc_app_overview", "marked": True, "enabled": True}
CREATE = Item(
    "command",
    "Create the MCP principal of acme-sales.overview in prod",
    "vecten app connect acme-sales.overview --env prod",
)
CONNECT = Item(
    "command",
    "Connect acme-sales.overview to the MCP in prod; if it is connected already, this "
    "rotates its secret and the app needs a ship",
    "vecten app connect acme-sales.overview --env prod",
)


def test_signed_in_next_offers_connect_where_the_mcp_lists_no_principal(
    invoke, project, fake_admin, tmp_path, monkeypatch
):
    config = deployed(project)
    signed_in(tmp_path / "xdg" / "vecten" / "tokens.json", config, project)
    monkeypatch.setattr(FakeAdmin, "listing", listed(None))
    items = post_deploy_items(project, config, "prod", CLEAN)
    assert CREATE in items and CONNECT not in items
    assert not [i for i in items if "acme-sales.warehouse" in i.resolution]
    monkeypatch.setattr(FakeAdmin, "listing", listed(MARKED))
    assert CREATE not in post_deploy_items(project, config, "prod", CLEAN)


def test_signed_in_next_offers_nothing_for_a_connected_app_this_checkout_never_stored(
    project, fake_admin, tmp_path, monkeypatch
):
    config = deployed(project)
    signed_in(tmp_path / "xdg" / "vecten" / "tokens.json", config, project)
    monkeypatch.setattr(FakeAdmin, "listing", listed(MARKED))
    items = post_deploy_items(project, config, "prod", CLEAN)
    assert not [i for i in items if "app connect" in i.resolution]


def test_signed_in_next_leaves_an_unmarked_principal_to_access_health(
    project, fake_admin, tmp_path, monkeypatch
):
    config = deployed(project)
    signed_in(tmp_path / "xdg" / "vecten" / "tokens.json", config, project)
    monkeypatch.setattr(FakeAdmin, "listing", listed({**MARKED, "marked": False}))
    items = post_deploy_items(project, config, "prod", CLEAN)
    assert not [i for i in items if "app connect" in i.resolution]


def test_next_falls_back_to_the_local_record_when_the_mcp_cannot_answer(
    invoke, project, fake_admin, tmp_path, monkeypatch
):
    config = deployed(project)
    signed_in(tmp_path / "xdg" / "vecten" / "tokens.json", config, project)
    monkeypatch.setattr(FakeAdmin, "listing", Failure(ExitCode.TOOL_FAILED, "MCP request failed"))
    assert CONNECT in post_deploy_items(project, config, "prod", CLEAN)
    assert connect(invoke, "--env", "prod").exit_code == 0
    assert CONNECT not in post_deploy_items(project, load_config(project), "prod", CLEAN)


def test_next_asks_for_sign_in_before_connecting(project, tmp_path):
    config = deployed(project)
    items = post_deploy_items(project, config, "prod", CLEAN)
    titles = [item.title for item in items]
    assert titles.index("Sign in to the MCP as an admin") < titles.index(CONNECT.title)
