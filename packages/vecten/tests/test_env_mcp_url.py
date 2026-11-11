"""Every CLI path that reaches or names a deployed MCP uses the environment's own domain.

The project has two environments on different domains: `prod` under vecten.toml's
`[deploy] domain` and `staging` under the domain its own tfvars sets.
"""

import json
import time

import httpx
import pytest

from vecten.deploy.tfvars import env_tfvars_path
from vecten.mcp_client import TokenCache, login_record
from vecten.next.gitstate import GitState
from vecten.next.post_deploy import post_deploy_items
from vecten_shared.config import EnvironmentFacts, load_config, save_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_record

from .deploy_fixtures import COMMIT, render_aws
from .scenarios.platform import use_mock_cloud
from .test_next_deploy import render_mcp_only_deploy

PROD_DOMAIN = "data.acme.com"
STAGING_DOMAIN = "acme-staging.net"
DOMAINS = {"prod": PROD_DOMAIN, "staging": STAGING_DOMAIN}
ENVS = pytest.mark.parametrize("env", sorted(DOMAINS))
ADMIN = "ada@acme.test"
CLEAN = GitState(has_repo=True, has_origin=True, dirty=False, ahead=False)


def add_staging(root):
    """A `staging` environment copied from prod's tfvars onto a domain of its own."""
    prod = env_tfvars_path(root, "prod").read_text()
    assert f'"{PROD_DOMAIN}"' in prod
    staging = prod.replace('"prod"', '"staging"').replace(f'"{PROD_DOMAIN}"', f'"{STAGING_DOMAIN}"')
    staging = staging.replace('"acme.com"', f'"{STAGING_DOMAIN}"')
    env_tfvars_path(root, "staging").write_text(staging)


def deploy_both(root):
    config = load_config(root)
    for env in DOMAINS:
        facts = config.environments.setdefault(env, EnvironmentFacts())
        facts.bootstrapped = True
        facts.state_bucket = "mock-state"
        facts.deployed_commit = COMMIT
    save_config(root, config)


@pytest.fixture
def project(tmp_path, monkeypatch):
    use_mock_cloud(monkeypatch, tmp_path / "cloud")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = render_aws(tmp_path / "proj")
    add_staging(root)
    config = load_config(root)
    config.mcp.auth = "password"
    config.mcp.admins = [ADMIN]
    save_config(root, config)
    deploy_both(root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def mcp_hosts(monkeypatch):
    """The hosts the CLI's HTTP client reaches, answered by a stand-in MCP."""
    hosts = []

    def handle(request):
        hosts.append(request.url.host)
        path = request.url.path
        if path == "/register":
            return httpx.Response(201, json={"client_id": "cli-1"})
        if path == "/device":
            device = {
                "device_code": "hidden",
                "user_code": "U",
                "verification_uri": f"https://{request.url.host}/device/verify",
                "expires_in": 600,
                "interval": 5,
            }
            return httpx.Response(200, json=device)
        operation = path.removeprefix("/api/admin/")
        data = {} if operation in {"grant", "reconcile"} else []
        return httpx.Response(200, json={"ok": True, "data": data})

    monkeypatch.setattr(
        "vecten.mcp_client.http_client",
        lambda *a, **k: httpx.Client(transport=httpx.MockTransport(handle)),
    )
    return hosts


def sign_in(env):
    session = {
        "access_token": "a-token",
        "refresh_token": "r-token",
        "client_id": "cli-1",
        "expires_at": time.time() + 3600,
    }
    TokenCache().save(f"https://mcp.{DOMAINS[env]}", session)


@ENVS
@pytest.mark.parametrize(
    "command",
    [
        ("user", "list"),
        ("group", "list"),
        ("grant", "--schema", "marts", "--group", "analysts"),
        ("reconcile",),
        ("login",),
    ],
    ids=" ".join,
)
def test_admin_commands_reach_the_environments_own_mcp(invoke, project, mcp_hosts, env, command):
    sign_in(env)
    result = invoke(*command, "--env", env, "--json")
    assert result.exit_code == 0, result.stdout
    assert mcp_hosts and set(mcp_hosts) == {f"mcp.{DOMAINS[env]}"}


@ENVS
def test_issues_read_the_environments_own_mcp(project, mcp_hosts, env):
    from vecten.issues.build import _admin

    sign_in(env)
    _admin(project, env)("issues", {})
    assert set(mcp_hosts) == {f"mcp.{DOMAINS[env]}"}


@ENVS
def test_a_session_is_kept_per_environment(project, env):
    from vecten.mcp_client import AdminClient, endpoint

    assert endpoint(project, env) == f"https://mcp.{DOMAINS[env]}"
    assert AdminClient(project, env).issuer == f"https://mcp.{DOMAINS[env]}"


@ENVS
def test_the_admin_link_names_the_environments_own_mcp(invoke, project, env):
    result = invoke("mcp", "admin-link", "--env", env, "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["url"] == f"https://mcp.{DOMAINS[env]}/set-password"


@ENVS
def test_the_connection_names_the_environments_own_mcp(invoke, project, env):
    result = invoke("mcp", "connection", "--env", env, "--json")
    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)
    assert data["url"] == f"https://mcp.{DOMAINS[env]}/mcp"
    assert f"vecten https://mcp.{DOMAINS[env]}/mcp" in data["steps"][1]
    text = invoke("mcp", "connection", "--env", env).stdout
    assert f"https://mcp.{DOMAINS[env]}/mcp" in text
    other = DOMAINS["staging" if env == "prod" else "prod"]
    assert f"mcp.{other}" not in text


@ENVS
def test_the_provisioner_script_trusts_the_environments_own_issuer(tmp_path, env):
    from vecten.mcp_onboarding import setup_script

    root = render_mcp_only_deploy(tmp_path, "snowflake://VECTEN_ADMIN@acct/analytics")
    add_staging(root)
    sql = setup_script(root, env)["sql"]
    assert f"EXTERNAL_OAUTH_ISSUER = 'https://mcp.{DOMAINS[env]}'" in sql
    other = DOMAINS["staging" if env == "prod" else "prod"]
    assert f"mcp.{other}" not in sql


@ENVS
def test_next_asks_to_register_the_environments_own_callback(project, env):
    config = load_config(project)
    config.mcp.auth = "oidc"
    config.mcp.oidc_issuer = "https://id.acme.com"
    config.mcp.oidc_client_id = "vecten"
    items = post_deploy_items(project, config, env, CLEAN)
    assert f"Register https://mcp.{DOMAINS[env]}/oidc/callback " in items[0].title


@ENVS
def test_next_sees_the_session_saved_for_the_environments_own_mcp(project, env):
    config = load_config(project)
    config.environments[env].admin_link_issued = True
    sign_in(env)
    write_record(project, login_record(env), {"client_id": "cli-1"})
    titles = [item.title for item in post_deploy_items(project, config, env, CLEAN)]
    assert "Sign in to the MCP as an admin" not in titles


def test_an_environment_without_tfvars_is_refused_with_how_to_add_it(project):
    from vecten.mcp_client import endpoint

    env_tfvars_path(project, "staging").unlink()
    with pytest.raises(Failure) as caught:
        endpoint(project, "staging")
    assert caught.value.code == ExitCode.REFUSED
    assert "deploy/envs/staging.tfvars" in caught.value.message


def test_the_laptop_keeps_its_local_mcp(project):
    from vecten.mcp_client import endpoint

    assert endpoint(project, "local") == "https://mcp.localhost:8443"
