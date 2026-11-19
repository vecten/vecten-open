import json

from vecten.deploy.tfvars import env_tfvars_path
from vecten_shared.config import load_config, save_config

from .fixture_project import render_fixture


def deploy(root, env="prod"):
    config = load_config(root)
    config.environments[env].deployed_commit = "abc123"
    save_config(root, config)


def test_connection_prints_url_and_records(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    deploy(root)
    monkeypatch.chdir(root)
    result = invoke("mcp", "connection", "--env", "prod", "--json")
    assert result.exit_code == 0, result.output
    assert result.stdout.count("\n") == 1
    data = json.loads(result.output)
    assert data["url"] == "https://mcp.data.acme.com/mcp" and "Claude Code" in "\n".join(
        data["steps"]
    )
    assert (
        "claude mcp add --transport http vecten https://mcp.data.acme.com/mcp" in data["steps"][1]
    )
    assert data["recorded"] is True
    assert load_config(root).environments["prod"].connection_shown is True


def test_connection_before_the_deploy_prints_the_url_without_recording(
    invoke, tmp_path, monkeypatch
):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    monkeypatch.chdir(root)
    result = invoke("mcp", "connection", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["url"] == "https://mcp.data.acme.com/mcp" and data["recorded"] is False
    assert "prod" not in load_config(root).environments
    prod = env_tfvars_path(root, "prod").read_text()
    staging = prod.replace('"prod"', '"staging"').replace('"data.acme.com"', '"staging.acme.com"')
    env_tfvars_path(root, "staging").write_text(staging)
    text = invoke("mcp", "connection", "--env", "staging")
    assert text.exit_code == 0, text.stderr
    assert "https://mcp.staging.acme.com/mcp" in text.stdout
    assert "not recorded" in text.stdout and "staging has not been deployed" in text.stdout
    deploy(root, "staging")
    assert json.loads(invoke("mcp", "connection", "--env", "staging", "--json").stdout)["recorded"]
    config = load_config(root)
    assert config.environments["staging"].connection_shown is True
    assert "prod" not in config.environments


def test_connection_text_lists_the_steps(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    monkeypatch.chdir(root)
    result = invoke("mcp", "connection")
    assert result.exit_code == 0, result.stderr
    assert "https://mcp.data.acme.com/mcp" in result.stdout
    assert "Claude Desktop" in result.stdout and "vecten user add" in result.stdout


def test_connection_refused_without_deploy_layer(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    assert invoke("mcp", "connection").exit_code == 4
    assert "prod" not in load_config(root).environments


def test_connection_refused_without_the_mcp_component(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    config = load_config(root)
    config.components = ["platform"]
    save_config(root, config)
    monkeypatch.chdir(root)
    r = invoke("mcp", "connection", "--json")
    assert r.exit_code == 4 and "mcp" in json.loads(r.stdout)["error"]


def test_connection_refused_outside_a_project_and_for_a_bad_env(invoke, tmp_path, monkeypatch):
    assert invoke("mcp", "connection").exit_code == 4
    root = render_fixture(tmp_path, "postgres", deploy=True)
    monkeypatch.chdir(root)
    assert invoke("mcp", "connection", "--env", "local").exit_code == 2
