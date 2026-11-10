"""`vecten mcp admin-link`: a one-time set-password link written to the cloud secrets store."""

import json

import pytest

from vecten_shared.config import load_config, save_config

from .deploy_fixtures import COMMIT, render_aws, set_facts
from .scenarios.platform import use_mock_cloud

ADMIN = "ada@acme.test"


@pytest.fixture
def project(tmp_path, monkeypatch):
    use_mock_cloud(monkeypatch, tmp_path / "cloud")
    root = render_aws(tmp_path / "proj")
    monkeypatch.chdir(root)
    config = load_config(root)
    config.mcp.auth = "password"
    config.mcp.admins = [ADMIN]
    save_config(root, config)
    set_facts(root, bootstrapped=True, state_bucket="mock-state")
    return root


@pytest.fixture
def deployed(project):
    set_facts(project, deployed_commit=COMMIT)
    return project


def stored(root):
    from vecten_mock_cloud import MockCloud

    plane = MockCloud(root, "prod")
    if "MCP_ADMIN_LINK" not in plane.list_secrets():
        return None
    return json.loads(plane.get_secret("MCP_ADMIN_LINK"))


def run(invoke, *args):
    result = invoke("mcp", "admin-link", *args, "--json")
    return result.exit_code, json.loads(result.stdout)


def test_issues_a_link_for_the_first_admin(invoke, deployed):
    code, doc = run(invoke, "--env", "prod")
    assert code == 0, doc
    # The token stays out of the URL, so no access log on the way holds it.
    assert doc["url"] == "https://mcp.data.acme.com/set-password"
    assert doc["email"] == ADMIN and doc["env"] == "prod" and doc["expires_in"] == 900
    secret = stored(deployed)
    assert secret["email"] == ADMIN
    assert doc["setup_token"] == secret["token"]
    assert isinstance(secret["issued_at"], float)


def test_an_issued_link_is_recorded_for_next(invoke, deployed):
    assert load_config(deployed).environments["prod"].admin_link_issued is False
    code, doc = run(invoke, "--env", "prod")
    assert code == 0, doc
    assert load_config(deployed).environments["prod"].admin_link_issued is True


def test_an_issued_link_is_recorded_for_the_admin(invoke, deployed):
    from vecten.password_links import read_links

    code, doc = run(invoke, "--env", "prod")
    assert code == 0, doc
    assert read_links(deployed, "prod") == [{"id": None, "email": ADMIN, "issued": True}]


def test_a_corrupt_record_stops_the_link_before_the_secret_is_set(invoke, deployed):
    path = deployed / ".vecten" / "records" / "password-links" / "prod.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    result = invoke("mcp", "admin-link", "--env", "prod", "--json")
    assert result.exit_code == 6
    assert stored(deployed) is None
    assert load_config(deployed).environments["prod"].admin_link_issued is False


def test_text_output_says_once_fifteen_minutes_and_the_email(invoke, deployed):
    result = invoke("mcp", "admin-link", "--env", "prod")
    assert result.exit_code == 0, result.stdout
    assert "https://mcp.data.acme.com/set-password" in result.stdout
    assert "?token=" not in result.stdout
    assert stored(deployed)["token"] in result.stdout
    assert "once" in result.stdout and "15 minutes" in result.stdout
    assert ADMIN in result.stdout


def test_email_matches_admins_case_insensitively(invoke, deployed):
    code, doc = run(invoke, "--env", "prod", "--email", "Ada@Acme.Test")
    assert code == 0, doc
    assert stored(deployed)["email"] == ADMIN


def test_oidc_mode_is_refused_before_any_write(invoke, deployed):
    config = load_config(deployed)
    config.mcp.auth = "oidc"
    save_config(deployed, config)
    code, doc = run(invoke, "--env", "prod")
    assert code == 4 and "password" in doc["error"]
    assert stored(deployed) is None


def test_an_email_outside_admins_is_refused(invoke, deployed):
    code, doc = run(invoke, "--env", "prod", "--email", "eve@acme.test")
    assert code == 4 and "eve@acme.test" in doc["error"]
    assert stored(deployed) is None


def test_no_admins_is_refused(invoke, deployed):
    config = load_config(deployed)
    config.mcp.admins = []
    save_config(deployed, config)
    code, doc = run(invoke, "--env", "prod")
    assert code == 4
    assert stored(deployed) is None


def test_an_undeployed_environment_is_refused(invoke, project):
    code, doc = run(invoke, "--env", "prod")
    assert code == 4 and "deploy" in doc["error"]
    assert stored(project) is None


def test_local_is_refused(invoke, deployed):
    code, doc = run(invoke, "--env", "local")
    assert code == 4 and "MCP_ADMIN_SETUP_TOKEN" in doc["error"]
    assert stored(deployed) is None


def test_a_project_without_mcp_is_refused(invoke, deployed):
    config = load_config(deployed)
    config.components = [c for c in config.components if c != "mcp"]
    save_config(deployed, config)
    code, doc = run(invoke, "--env", "prod")
    assert code == 4 and "mcp" in doc["error"]
    assert stored(deployed) is None


def test_the_token_never_reaches_project_records(invoke, deployed):
    code, doc = run(invoke, "--env", "prod")
    assert code == 0, doc
    token = stored(deployed)["token"]
    for path in deployed.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(errors="ignore"), path
