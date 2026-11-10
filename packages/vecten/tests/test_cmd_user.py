"""`vecten user add`, `password-link`, `disable` and `delete` keep the record of links owed."""

import httpx
import pytest

from vecten.mcp_client import TokenCache
from vecten.next.gitstate import GitState
from vecten.next.items import Item
from vecten.next.post_deploy import post_deploy_items
from vecten.password_links import owed, read_links
from vecten_shared.config import load_config, save_config
from vecten_shared.output import ExitCode, Failure

from .fixture_project import render_fixture
from .test_next_deploy import signed_in

GRACE = {"id": "p-grace", "email": "grace@acme.test", "issued": False}


class FakeAdmin:
    calls: list = []
    failure: Failure | None = None

    def __init__(self, root, env):
        pass

    def call(self, operation, data=None):
        if self.failure:
            raise self.failure
        self.calls.append((operation, data))
        if operation == "users/create":
            return {"id": "p-grace", "email": data["email"]}
        if operation == "users/password-link":
            return {"url": "https://mcp.acme.test/set-password", "setup_token": "tok"}
        return {}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = render_fixture(tmp_path, "postgres", deploy=True, domain="acme.test")
    config = load_config(root)
    config.mcp.auth = "password"
    save_config(root, config)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def fake_admin(project, monkeypatch):
    monkeypatch.setattr(FakeAdmin, "calls", [])
    monkeypatch.setattr(FakeAdmin, "failure", None)
    monkeypatch.setattr("vecten.mcp_client.AdminClient", FakeAdmin)


def add_grace(invoke):
    result = invoke(
        "user", "add", "--email", "grace@acme.test", "--name", "Grace Hopper", "--env", "prod"
    )
    return result


def test_user_add_records_the_link_owed(invoke, project, fake_admin):
    assert add_grace(invoke).exit_code == 0
    assert owed(project, "prod") == [GRACE]
    result = invoke("user", "password-link", "p-grace", "--env", "prod", "--json")
    assert result.exit_code == 0, result.stdout
    assert owed(project, "prod") == []


def test_user_add_under_oidc_records_no_link(invoke, project, fake_admin):
    config = load_config(project)
    config.mcp.auth = "oidc"
    save_config(project, config)
    assert add_grace(invoke).exit_code == 0
    assert read_links(project, "prod") == []


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_disable_and_delete_drop_the_link(invoke, project, fake_admin, action):
    assert add_grace(invoke).exit_code == 0
    assert read_links(project, "prod") == [GRACE]
    result = invoke("user", action, "p-grace", "--env", "prod")
    assert result.exit_code == 0, result.stdout
    assert read_links(project, "prod") == []


def test_a_corrupt_record_stops_user_add_before_the_mcp(invoke, project, fake_admin):
    path = project / ".vecten" / "records" / "password-links" / "prod.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    assert add_grace(invoke).exit_code == ExitCode.CONFIG
    assert FakeAdmin.calls == []


def test_a_refused_add_records_nothing(invoke, project, fake_admin, monkeypatch):
    monkeypatch.setattr(
        FakeAdmin, "failure", Failure(ExitCode.REFUSED, "MCP refused users/create (HTTP 400)")
    )
    assert add_grace(invoke).exit_code == ExitCode.REFUSED
    assert read_links(project, "prod") == []


def test_user_add_with_a_rejected_session_asks_for_login(invoke, project, monkeypatch, tmp_path):
    config = load_config(project)
    facts = config.environments["prod"]
    facts.bootstrapped = True
    facts.deployed_commit = "abc123"
    config.mcp.admins = ["ada@acme.test"]
    facts.admin_link_issued = True
    save_config(project, config)
    cache = tmp_path / "xdg" / "vecten" / "tokens.json"
    signed_in(cache, config, project)

    def reject(request):
        assert request.url.path == "/api/admin/users/create"
        return httpx.Response(401, json={"error": "invalid_token"})

    def mock_client(root=None, env=None, transport=None):
        return httpx.Client(transport=httpx.MockTransport(reject))

    monkeypatch.setattr("vecten.mcp_client.http_client", mock_client)
    result = invoke(
        "user", "add", "--email", "grace@acme.test", "--name", "Grace Hopper",
        "--env", "prod", "--json",
    )  # fmt: skip
    assert result.exit_code == 3, result.stdout
    assert "vecten login" in result.stdout
    assert TokenCache().load("https://mcp.acme.test") is None
    config = load_config(project)
    clean = GitState(has_repo=True, has_origin=True, dirty=False, ahead=False)
    assert Item(
        "command", "Sign in to the MCP as an admin", "vecten login --env prod"
    ) in post_deploy_items(project, config, "prod", clean)


def corrupt_record(project):
    path = project / ".vecten" / "records" / "password-links" / "prod.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")


def test_a_corrupt_record_stops_password_link_before_the_mcp(invoke, project, fake_admin):
    corrupt_record(project)
    result = invoke("user", "password-link", "p-grace", "--env", "prod", "--json")
    assert result.exit_code == ExitCode.CONFIG
    assert FakeAdmin.calls == []


@pytest.mark.parametrize("action", ["disable", "delete"])
def test_a_corrupt_record_stops_disable_and_delete_before_the_mcp(
    invoke, project, fake_admin, action
):
    corrupt_record(project)
    result = invoke("user", action, "p-grace", "--env", "prod")
    assert result.exit_code == ExitCode.CONFIG
    assert FakeAdmin.calls == []
