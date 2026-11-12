import json
import time

import pytest

from vecten.intake.answers import ANSWER_KEYS, write_answer
from vecten.intake.facts import set_oidc_registered_fact
from vecten.mcp_client import login_record
from vecten.next.deploy import (
    deploy_gate_items,
    deploy_prerequisite_items,
    deploy_question_items,
)
from vecten.next.existing_warehouse import existing_warehouse_items
from vecten.next.gitstate import GitState
from vecten.next.items import Item
from vecten.next.post_deploy import post_deploy_items
from vecten.password_links import added, admin_link_issued, issued
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import AccessDecl, load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_record

from .fixture_project import render_fixture
from .scaffold_matrix import DEPLOY
from .test_next_project import (
    LOCAL_PROVISIONER_ENV,
    POSTGRES_URL,
    SNOWFLAKE_URL,
    render_mcp_only,
)

CLOUD_Q = ANSWER_KEYS["cloud"].question
DOMAIN_Q = ANSWER_KEYS["domain"].question
GITHUB_Q = ANSWER_KEYS["github_repository"].question
NO_ACCOUNT = "Answer none if you do not have one yet."
CLOUDS = ("aws", "scaleway", "none")
CLOUD = Item("ask", CLOUD_Q, CLOUD_Q, answer_keys=("cloud",), allowed=CLOUDS)
DOMAIN = Item("ask", DOMAIN_Q, f"{DOMAIN_Q} {NO_ACCOUNT}", answer_keys=("domain", "dns_zone"))
GITHUB = Item("ask", GITHUB_Q, f"{GITHUB_Q} {NO_ACCOUNT}", answer_keys=("github_repository",))
INIT_DEPLOY = Item("command", "Render the deploy layer", "vecten init --deploy")
ACCOUNT_Q = ANSWER_KEYS["snowflake_account"].question
ACCOUNT = Item("ask", ACCOUNT_Q, ACCOUNT_Q, answer_keys=("snowflake_account",))
DEPLOY_ANSWERS = {
    "cloud": "aws",
    "domain": "data.acme.com",
    "dns_zone": "acme.com",
    "github_repository": "acme/data",
}

GITHUB_INIT = Item("command", "Put the project on GitHub", "vecten github init")
BOOTSTRAP = Item("command", "Bootstrap the prod environment", "vecten bootstrap --env prod")
COPY_HUBSPOT = Item(
    "command",
    "Copy HUBSPOT_API_KEY to prod",
    "vecten secret set HUBSPOT_API_KEY --env prod --from-local",
)
PUSH = Item("command", "Push the project's changes to GitHub", "vecten github push")
PROVISIONER_SECRETS = {
    POSTGRES_URL: Item(
        "command",
        "Store the provisioner credentials",
        "vecten secret set MCP_PROVISIONER_PASSWORD --env prod --from-local"
        " && vecten secret set MCP_IMPERSONATOR_PASSWORD --env prod --from-local",
    ),
    SNOWFLAKE_URL: Item(
        "command",
        "Store the provisioner credentials",
        "vecten secret set MCP_PROVISIONER_PRIVATE_KEY --env prod --from-local",
    ),
}
GATE = Item(
    "ask",
    "Deploy to prod? This creates resources your cloud provider bills you for.",
    "vecten deploy --env prod",
    allowed=("yes", "no"),
)
CLEAN = GitState(has_repo=True, has_origin=True, dirty=False, ahead=False)
NO_ORIGIN = GitState(has_repo=True, has_origin=False, dirty=True, ahead=True)

OIDC = Item(
    "ask",
    "Which OIDC issuer and client id should sign people in?",
    "Which OIDC issuer URL and client id should the MCP use to sign people in? "
    '(Set [mcp] auth = "password" with `vecten answer mcp.auth password` to opt out.)',
    answer_keys=("mcp.oidc_issuer", "mcp.oidc_client_id"),
)
ADMIN = Item(
    "ask",
    "Who is the first admin?",
    "Which email address should be the first admin of the MCP?",
    answer_keys=("mcp.admins",),
)
FIRST_USER = Item(
    "ask",
    "Who should have access first? Give an email address and a name.",
    'vecten user add --email <email> --name "<name>" --env prod',
)
CONNECTION = Item(
    "command", "Show the MCP URL and how to connect a client", "vecten mcp connection --env prod"
)
ALERT = Item(
    "ask",
    "Should sync failures be posted to a webhook? "
    "On yes, run `vecten secret set ALERT_WEBHOOK_URL --env prod` first.",
    "vecten answer alert_webhook.prod <yes|no>",
    answer_keys=("alert_webhook.prod",),
    allowed=("yes", "no"),
)
APPLY_SALES = Item(
    "command",
    "Grant sales access to raw_demo",
    "vecten group add sales --env prod && vecten grant --schema raw_demo --group sales --env prod",
)


@pytest.fixture
def local_only(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    return root, load_config(root)


@pytest.fixture
def deploy_layer(tmp_path):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    return root, load_config(root)


def render_mcp_only_deploy(tmp_path, url):
    root = tmp_path / "proj"
    answers = {
        "name": "acme",
        "users": 3,
        "has_warehouse": "yes",
        "warehouse_url": url,
        **DEPLOY,
        "cloud": "aws",
    }
    render_project(root, ScaffoldParams.from_answers(answers, {}))
    return root


def deployed(config, env="prod"):
    config.environments[env].bootstrapped = True
    config.environments[env].deployed_commit = "abc123"
    return config


# Deploy questions


def test_no_answers_asks_the_cloud(local_only):
    root, config = local_only
    assert deploy_question_items(root, config, {}) == [CLOUD]


def test_cloud_none_carries_estimate_and_guide(local_only):
    root, config = local_only
    items = deploy_question_items(root, config, {"cloud": "none"})
    estimate = "vecten estimate --size small --components platform --warehouse postgres"
    assert items == [
        Item(
            "ask",
            CLOUD_Q,
            CLOUD_Q,
            answer_keys=("cloud",),
            allowed=("aws", "scaleway", "none"),
            show_first=(estimate, "vecten guide cloud"),
        )
    ]


@pytest.mark.parametrize(
    ("url", "warehouse"), [(POSTGRES_URL, "postgres"), (SNOWFLAKE_URL, "snowflake")]
)
def test_cloud_estimate_follows_an_existing_warehouse_project(tmp_path, url, warehouse):
    root = render_mcp_only(tmp_path, url)
    config = load_config(root)
    config.size = "medium"
    items = deploy_question_items(root, config, {"cloud": "none"})
    assert items[0].show_first == (
        f"vecten estimate --size medium --components mcp --warehouse {warehouse}",
        "vecten guide cloud",
    )


def test_free_text_asks_say_how_to_record_no_account(local_only):
    root, config = local_only
    answers = {k: v for k, v in DEPLOY_ANSWERS.items() if k != "github_repository"}
    for item in (
        deploy_question_items(root, config, {"cloud": "aws"})[0],
        deploy_question_items(root, config, answers)[0],
    ):
        assert item.title == ANSWER_KEYS[item.answer_keys[0]].question
        assert item.resolution == f"{item.title} Answer none if you do not have one yet."


@pytest.mark.parametrize("answers", [{"cloud": "aws"}, {"cloud": "aws", "domain": "data.acme.com"}])
def test_cloud_answered_asks_the_domain_with_both_keys(local_only, answers):
    root, config = local_only
    assert deploy_question_items(root, config, answers) == [DOMAIN]


def test_domain_none_shows_the_domain_guide(local_only):
    root, config = local_only
    items = deploy_question_items(root, config, {"cloud": "aws", "domain": "none"})
    assert items[0].show_first == ("vecten guide domain",)
    assert items[0].answer_keys == ("domain", "dns_zone")


def test_github_asked_last_and_none_shows_its_guide(local_only):
    root, config = local_only
    answers = {k: v for k, v in DEPLOY_ANSWERS.items() if k != "github_repository"}
    assert deploy_question_items(root, config, answers) == [GITHUB]
    items = deploy_question_items(root, config, {**answers, "github_repository": "none"})
    assert items[0].show_first == ("vecten guide github",)


def test_all_answered_reports_init_deploy(local_only):
    root, config = local_only
    assert deploy_question_items(root, config, DEPLOY_ANSWERS) == [INIT_DEPLOY]


def test_a_snowflake_platform_is_asked_its_account_before_the_deploy_layer(tmp_path):
    root = render_fixture(tmp_path, "snowflake")
    config = load_config(root)
    assert deploy_question_items(root, config, DEPLOY_ANSWERS) == [ACCOUNT]
    answers = {**DEPLOY_ANSWERS, "snowflake_account": "ACME-PROD"}
    assert deploy_question_items(root, config, answers) == [INIT_DEPLOY]


def test_an_existing_snowflake_warehouse_is_not_asked_its_account(tmp_path):
    root = render_mcp_only(tmp_path, SNOWFLAKE_URL)
    assert deploy_question_items(root, load_config(root), DEPLOY_ANSWERS) == [INIT_DEPLOY]


def test_a_deploy_layer_without_its_snowflake_account_asks_it_first(tmp_path):
    root = render_fixture(tmp_path, "snowflake", deploy=True, account=None)
    config = load_config(root)
    config.environments["prod"].bootstrapped = True
    assert deploy_prerequisite_items(root, config, "prod", CLEAN)[0] == ACCOUNT
    write_answer(root, "snowflake_account", "ACME-PROD")
    assert deploy_prerequisite_items(root, config, "prod", CLEAN)[0] == INIT_DEPLOY


def test_rendered_deploy_layer_asks_nothing(deploy_layer):
    root, config = deploy_layer
    assert deploy_question_items(root, config, {}) == []


# Prerequisites and the gate


def test_prerequisites_order_without_origin(deploy_layer):
    root, config = deploy_layer
    (root / ".env").write_text("HUBSPOT_API_KEY=x\n")
    items = deploy_prerequisite_items(root, config, "prod", NO_ORIGIN)
    assert items == [GITHUB_INIT, BOOTSTRAP, COPY_HUBSPOT]


def test_no_origin_reports_github_init_and_no_push(deploy_layer):
    root, config = deploy_layer
    config.environments["prod"].bootstrapped = True
    assert deploy_prerequisite_items(root, config, "prod", NO_ORIGIN) == [GITHUB_INIT]


def test_origin_not_bootstrapped_reports_bootstrap_first(deploy_layer):
    root, config = deploy_layer
    dirty = GitState(has_repo=True, has_origin=True, dirty=True, ahead=False)
    assert deploy_prerequisite_items(root, config, "prod", dirty) == [BOOTSTRAP, PUSH]


def test_local_secret_until_copied(deploy_layer):
    root, config = deploy_layer
    config.environments["prod"].bootstrapped = True
    (root / ".env").write_text("HUBSPOT_API_KEY=x\nLOADER_PASSWORD=local\n")
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == [COPY_HUBSPOT]
    write_record(root, "secrets-prod", {"env": "prod", "copied": ["HUBSPOT_API_KEY"]})
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == []


def test_local_secret_without_a_value_is_not_offered(deploy_layer):
    root, config = deploy_layer
    config.environments["prod"].bootstrapped = True
    (root / ".env").write_text("BARE\nEMPTY=\nHUBSPOT_API_KEY=x\n")
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == [COPY_HUBSPOT]


@pytest.mark.parametrize(
    "git",
    [
        GitState(has_repo=True, has_origin=True, dirty=True, ahead=False),
        GitState(has_repo=True, has_origin=True, dirty=False, ahead=True),
    ],
)
def test_dirty_or_ahead_reports_push(deploy_layer, git):
    root, config = deploy_layer
    config.environments["prod"].bootstrapped = True
    assert deploy_prerequisite_items(root, config, "prod", git) == [PUSH]


@pytest.mark.parametrize("url", [POSTGRES_URL, SNOWFLAKE_URL])
def test_deploy_items_never_copy_the_warehouse_admin_password(tmp_path, url):
    root = render_mcp_only_deploy(tmp_path, url)
    config = load_config(root)
    # The provisioner's cloud store item covers its names; the admin password stays local.
    (root / ".env").write_text("WAREHOUSE_ADMIN_PASSWORD=x\n" + LOCAL_PROVISIONER_ENV[url])
    dirty = GitState(has_repo=True, has_origin=True, dirty=True, ahead=False)
    store = PROVISIONER_SECRETS[url]
    items = deploy_prerequisite_items(root, config, "prod", dirty)
    assert items == [BOOTSTRAP, store, PUSH]
    config.environments["prod"].bootstrapped = True
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == [store]
    config.environments["prod"].provisioner_secrets_set = True
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == []


@pytest.mark.parametrize("url", [POSTGRES_URL, SNOWFLAKE_URL])
def test_deploy_items_never_copy_the_git_credential(tmp_path, url):
    root = render_mcp_only_deploy(tmp_path, url)
    config = load_config(root)
    (root / ".env").write_text(
        "GIT_CREDENTIALS=https://u:t@example.com\nMCP_GIT_CREDENTIALS=https://u:t@example.com\n"
        + LOCAL_PROVISIONER_ENV[url]
    )
    config.environments["prod"].bootstrapped = True
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == [PROVISIONER_SECRETS[url]]


@pytest.mark.parametrize("url", [POSTGRES_URL, SNOWFLAKE_URL])
def test_provisioner_script_item_names_the_deployed_env(tmp_path, url):
    root = render_mcp_only_deploy(tmp_path, url)
    (root / ".env").write_text(LOCAL_PROVISIONER_ENV[url])
    assert existing_warehouse_items(root, load_config(root), "prod") == [
        Item(
            "command",
            "Prepare the existing warehouse for the MCP",
            "vecten mcp provisioner-script --env prod",
        )
    ]


STORE_DEPLOY_USER = Item(
    "command",
    "Store the Snowflake deploy user's name in prod",
    "vecten secret set DEPLOY_SNOWFLAKE_USER --env prod",
)
STORE_DEPLOY_KEY = Item(
    "command",
    "Store the Snowflake deploy user's private key in prod, a PEM file on standard input",
    "vecten secret set DEPLOY_SNOWFLAKE_PRIVATE_KEY --env prod",
)


@pytest.fixture
def snowflake_deploy_layer(tmp_path):
    root = render_fixture(tmp_path, "snowflake", deploy=True)
    return root, load_config(root)


def test_snowflake_deploy_secrets_follow_bootstrap_and_precede_the_copies(snowflake_deploy_layer):
    root, config = snowflake_deploy_layer
    (root / ".env").write_text("HUBSPOT_API_KEY=x\n")
    dirty = GitState(has_repo=True, has_origin=True, dirty=True, ahead=False)
    items = deploy_prerequisite_items(root, config, "prod", dirty)
    assert items == [BOOTSTRAP, STORE_DEPLOY_USER, STORE_DEPLOY_KEY, COPY_HUBSPOT, PUSH]


def test_snowflake_deploy_secrets_vanish_once_recorded(snowflake_deploy_layer):
    root, config = snowflake_deploy_layer
    config.environments["prod"].bootstrapped = True
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == [
        STORE_DEPLOY_USER,
        STORE_DEPLOY_KEY,
    ]
    write_record(root, "secrets-prod", {"env": "prod", "stored": ["DEPLOY_SNOWFLAKE_USER"]})
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == [STORE_DEPLOY_KEY]
    write_record(
        root,
        "secrets-prod",
        {"env": "prod", "stored": ["DEPLOY_SNOWFLAKE_USER", "DEPLOY_SNOWFLAKE_PRIVATE_KEY"]},
    )
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == []


def test_a_copied_deploy_secret_counts_as_present(snowflake_deploy_layer):
    root, config = snowflake_deploy_layer
    config.environments["prod"].bootstrapped = True
    write_record(
        root,
        "secrets-prod",
        {"env": "prod", "copied": ["DEPLOY_SNOWFLAKE_USER", "DEPLOY_SNOWFLAKE_PRIVATE_KEY"]},
    )
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == []


def test_a_stored_local_secret_is_not_offered_for_copying(deploy_layer):
    root, config = deploy_layer
    config.environments["prod"].bootstrapped = True
    (root / ".env").write_text("HUBSPOT_API_KEY=x\n")
    write_record(root, "secrets-prod", {"env": "prod", "stored": ["HUBSPOT_API_KEY"]})
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == []


def test_snowflake_deploy_secrets_substitute_the_env(snowflake_deploy_layer):
    root, config = snowflake_deploy_layer
    items = deploy_prerequisite_items(root, config, "staging", CLEAN)
    assert [i.resolution for i in items] == [
        "vecten bootstrap --env staging",
        "vecten secret set DEPLOY_SNOWFLAKE_USER --env staging",
        "vecten secret set DEPLOY_SNOWFLAKE_PRIVATE_KEY --env staging",
    ]


@pytest.mark.parametrize("url", [POSTGRES_URL, SNOWFLAKE_URL])
def test_mcp_only_projects_need_no_snowflake_deploy_secrets(tmp_path, url):
    root = render_mcp_only_deploy(tmp_path, url)
    items = deploy_prerequisite_items(root, load_config(root), "prod", CLEAN)
    assert all("DEPLOY_SNOWFLAKE" not in i.resolution for i in items)


def test_platform_project_has_no_provisioner_secrets(deploy_layer):
    root, config = deploy_layer
    items = deploy_prerequisite_items(root, config, "prod", CLEAN)
    assert all("MCP_PROVISIONER_PASSWORD" not in i.resolution for i in items)


def test_provisioner_secrets_substitute_the_env(tmp_path):
    root = render_mcp_only_deploy(tmp_path, POSTGRES_URL)
    config = load_config(root)
    config.environments["staging"].bootstrapped = True
    items = deploy_prerequisite_items(root, config, "staging", CLEAN)
    assert items[0].title == "Store the provisioner credentials"
    assert items[0].resolution.count("--env staging") == 2


def test_all_clear_reports_the_gate(deploy_layer):
    root, config = deploy_layer
    config.environments["prod"].bootstrapped = True
    assert deploy_prerequisite_items(root, config, "prod", CLEAN) == []
    assert deploy_gate_items(config, "prod") == [GATE]
    assert GATE.allowed == ("yes", "no") and "bills" in GATE.title


def test_deployed_has_no_prerequisites_and_no_gate(deploy_layer):
    root, config = deploy_layer
    deployed(config)
    assert deploy_prerequisite_items(root, config, "prod", NO_ORIGIN) == []
    assert deploy_gate_items(config, "prod") == []


def test_no_deploy_layer_has_no_prerequisites_and_no_gate(local_only):
    root, config = local_only
    assert deploy_prerequisite_items(root, config, "prod", NO_ORIGIN) == []
    assert deploy_gate_items(config, "prod") == []
    assert "prod" not in config.environments


# Post-deploy


@pytest.fixture(autouse=True)
def token_cache(tmp_path, monkeypatch):
    """Each test's own token cache, so a session on this machine never shows through."""
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home / "vecten" / "tokens.json"


def signed_in(token_cache, config, root, env="prod", **session):
    """A session this project's login left: a usable cache entry and the project's record
    of its client; `session` overrides the entry's fields."""
    token_cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    issuer = f"https://mcp.{config.deploy.domain}"
    entry = {
        "access_token": "a-token",
        "refresh_token": "r-token",
        "client_id": "cli-1",
        "expires_at": time.time() + 3600,
        **session,
    }
    token_cache.write_text(json.dumps({issuer: entry}))
    token_cache.chmod(0o600)
    write_record(root, login_record(env), {"client_id": "cli-1"})


def register_item(config, env="prod"):
    return Item(
        "ask",
        f"Register https://mcp.{config.deploy.domain}/oidc/callback with your identity "
        "provider. If it gave you a client secret, store it with "
        f"`vecten secret set MCP_OIDC_CLIENT_SECRET --env {env}`.",
        f"vecten answer oidc_registered.{env} yes",
        answer_keys=(f"oidc_registered.{env}",),
        allowed=("yes",),
    )


ADMIN_LINK = Item(
    "command", "Give the first admin a password link", "vecten mcp admin-link --env prod"
)
LOGIN = Item("command", "Sign in to the MCP as an admin", "vecten login --env prod")


def oidc_ready(config):
    config.mcp.oidc_issuer = "https://id.acme.com"
    config.mcp.oidc_client_id = "vecten"
    config.mcp.admins = ["ana@acme.com"]
    return deployed(config)


def test_a_configured_oidc_asks_to_register_the_callback_before_anything_else(deploy_layer):
    root, config = deploy_layer
    config = oidc_ready(config)
    assert post_deploy_items(root, config, "prod", CLEAN) == [
        register_item(config),
        FIRST_USER,
        CONNECTION,
        ALERT,
    ]


def test_a_registered_callback_leads_to_sign_in_before_the_first_user(deploy_layer):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    assert post_deploy_items(root, config, "prod", CLEAN) == [LOGIN, FIRST_USER, CONNECTION, ALERT]


def test_password_mode_issues_the_admin_link_then_signs_in(deploy_layer):
    root, config = deploy_layer
    config.mcp.auth = "password"
    config.mcp.admins = ["ana@acme.com"]
    deployed(config)
    assert post_deploy_items(root, config, "prod", CLEAN) == [
        ADMIN_LINK,
        FIRST_USER,
        CONNECTION,
        ALERT,
    ]
    config.environments["prod"].admin_link_issued = True
    assert post_deploy_items(root, config, "prod", CLEAN) == [LOGIN, FIRST_USER, CONNECTION, ALERT]


def test_a_cached_session_needs_no_sign_in(deploy_layer, token_cache):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    signed_in(token_cache, config, root)
    assert post_deploy_items(root, config, "prod", CLEAN) == [FIRST_USER, CONNECTION, ALERT]


def test_an_unreadable_token_cache_reads_as_signed_out(deploy_layer, token_cache):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    signed_in(token_cache, config, root)
    token_cache.chmod(0o644)
    assert post_deploy_items(root, config, "prod", CLEAN)[0] == LOGIN


def _asks_for_login(root, config):
    return post_deploy_items(root, config, "prod", CLEAN)[0] == LOGIN


def test_a_session_left_by_another_project_asks_for_login(deploy_layer, token_cache):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    signed_in(token_cache, config, root, client_id="cli-other")
    assert _asks_for_login(root, config)


def test_a_session_that_can_no_longer_refresh_asks_for_login(deploy_layer, token_cache):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    signed_in(token_cache, config, root, expires_at=time.time() - 1, refresh_token="")
    assert _asks_for_login(root, config)


def test_a_login_record_without_its_session_asks_for_login(deploy_layer):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    write_record(root, login_record("prod"), {"client_id": "cli-1"})
    assert _asks_for_login(root, config)


def test_a_corrupt_login_record_counts_as_no_session(deploy_layer, token_cache):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    signed_in(token_cache, config, root)
    (root / ".vecten" / "records" / "login" / "prod.json").write_text("{")
    assert _asks_for_login(root, config)


def test_the_callback_answer_records_the_fact(deploy_layer):
    root, _config = deploy_layer
    set_oidc_registered_fact(root, "prod", "yes")
    assert load_config(root).environments["prod"].oidc_registered is True
    with pytest.raises(Failure) as caught:
        set_oidc_registered_fact(root, "prod", "no")
    assert caught.value.code == ExitCode.USAGE


def test_post_deploy_defaults_start_with_oidc(deploy_layer):
    root, config = deploy_layer
    assert post_deploy_items(root, deployed(config), "prod", CLEAN) == [
        OIDC,
        ADMIN,
        FIRST_USER,
        CONNECTION,
        ALERT,
    ]


@pytest.mark.parametrize(
    "git",
    [
        GitState(has_repo=True, has_origin=True, dirty=True, ahead=False),
        GitState(has_repo=True, has_origin=True, dirty=False, ahead=True),
    ],
)
def test_post_deploy_changes_are_pushed_first(deploy_layer, git):
    root, config = deploy_layer
    assert post_deploy_items(root, deployed(config), "prod", git) == [
        PUSH,
        OIDC,
        ADMIN,
        FIRST_USER,
        CONNECTION,
        ALERT,
    ]


def test_post_deploy_push_waits_for_the_deploy(deploy_layer):
    root, config = deploy_layer
    dirty = GitState(has_repo=True, has_origin=True, dirty=True, ahead=True)
    assert post_deploy_items(root, config, "prod", dirty) == []


def test_password_auth_skips_oidc(deploy_layer):
    root, config = deploy_layer
    config.mcp.auth = "password"
    assert post_deploy_items(root, deployed(config), "prod", CLEAN)[0] == ADMIN


def test_issuer_without_client_id_still_asks_oidc(deploy_layer):
    root, config = deploy_layer
    config.mcp.oidc_issuer = "https://id.acme.com"
    assert post_deploy_items(root, deployed(config), "prod", CLEAN)[0] == OIDC


def test_signed_in_with_oidc_set_asks_the_first_user(deploy_layer, token_cache):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    signed_in(token_cache, config, root)
    items = post_deploy_items(root, config, "prod", CLEAN)
    assert items[0] == FIRST_USER
    assert items[0].resolution.startswith('vecten user add --email <email> --name "<name>"')


def test_first_user_added_reports_access_then_connection_then_alert(deploy_layer, token_cache):
    root, config = deploy_layer
    config.mcp.auth = "password"
    config.mcp.admins = ["ana@acme.com"]
    config.connector("demo").access = AccessDecl(groups=["sales"])
    facts = deployed(config).environments["prod"]
    facts.first_user_added = True
    # Grants are admin calls too, so a lapsed session asks for sign-in first.
    assert post_deploy_items(root, config, "prod", CLEAN) == [
        LOGIN,
        APPLY_SALES,
        CONNECTION,
        ALERT,
    ]
    signed_in(token_cache, config, root)
    assert post_deploy_items(root, config, "prod", CLEAN) == [APPLY_SALES, CONNECTION, ALERT]
    facts.connector_access_applied = ["demo"]
    facts.connection_shown = True
    assert post_deploy_items(root, config, "prod", CLEAN) == [ALERT]
    facts.alert_webhook_asked = True
    assert post_deploy_items(root, config, "prod", CLEAN) == []


def link_item(email, principal_id):
    return Item(
        "command",
        f"Give {email} a password link",
        f"vecten user password-link {principal_id} --env prod",
    )


def password_ready(config):
    config.mcp.auth = "password"
    config.mcp.admins = ["ada@acme.test"]
    facts = deployed(config).environments["prod"]
    facts.admin_link_issued = True
    facts.first_user_added = True
    facts.connector_access_applied = ["demo"]
    return facts


def test_password_link_for_an_added_person(deploy_layer, token_cache):
    root, config = deploy_layer
    password_ready(config)
    signed_in(token_cache, config, root)
    added(root, "prod", "p-grace", "grace@acme.test")
    link = link_item("grace@acme.test", "p-grace")
    items = post_deploy_items(root, config, "prod", CLEAN)
    assert items[:2] == [link, CONNECTION]
    issued(root, "prod", "p-grace")
    assert link not in post_deploy_items(root, config, "prod", CLEAN)


def test_an_owed_link_brings_back_sign_in(deploy_layer):
    root, config = deploy_layer
    password_ready(config)
    added(root, "prod", "p-grace", "grace@acme.test")
    items = post_deploy_items(root, config, "prod", CLEAN)
    assert items[:2] == [LOGIN, link_item("grace@acme.test", "p-grace")]


def test_the_first_admin_is_owed_no_link(deploy_layer, token_cache):
    root, config = deploy_layer
    password_ready(config)
    signed_in(token_cache, config, root)
    admin_link_issued(root, "prod", "ada@acme.test")
    added(root, "prod", "p-ada", "ada@acme.test")
    titles = [i.title for i in post_deploy_items(root, config, "prod", CLEAN)]
    assert "Give ada@acme.test a password link" not in titles


def test_oidc_reports_no_password_link(deploy_layer, token_cache):
    root, config = deploy_layer
    config = oidc_ready(config)
    facts = config.environments["prod"]
    facts.oidc_registered = facts.first_user_added = True
    signed_in(token_cache, config, root)
    added(root, "prod", "p-grace", "grace@acme.test")
    titles = [i.title for i in post_deploy_items(root, config, "prod", CLEAN)]
    assert not any("password link" in t for t in titles)


def test_post_deploy_empty_before_deploy(deploy_layer):
    root, config = deploy_layer
    assert post_deploy_items(root, config, "prod", CLEAN) == []
    assert "prod" not in config.environments


def test_mcp_only_project_is_not_asked_for_a_sync_webhook(tmp_path):
    root = render_mcp_only(tmp_path)
    config = deployed(load_config(root))
    assert post_deploy_items(root, config, "prod", CLEAN) == [
        OIDC,
        ADMIN,
        FIRST_USER,
        CONNECTION,
    ]


def test_platform_only_project_has_no_post_deploy_items(local_only):
    root, config = local_only
    assert config.components == ["platform"]
    assert post_deploy_items(root, deployed(config), "prod", CLEAN) == []


def test_post_deploy_substitutes_the_env(deploy_layer):
    root, config = deploy_layer
    items = post_deploy_items(root, deployed(config, "staging"), "staging", CLEAN)
    assert items[-1].answer_keys == ("alert_webhook.staging",)
    assert all("--env prod" not in i.title + i.resolution for i in items)


# Reading facts never inserts an environment


def test_existing_warehouse_does_not_insert_the_env(tmp_path):
    root = render_mcp_only(tmp_path)
    config = load_config(root)
    existing_warehouse_items(root, config, "prod")
    assert "prod" not in config.environments


IDENTITY = Item(
    "command",
    "Set up the MCP's Snowflake identity",
    "vecten mcp provisioner-script --env prod",
)


def test_a_deployed_platform_snowflake_sets_up_the_mcp_identity_first(snowflake_deploy_layer):
    root, config = snowflake_deploy_layer
    deployed(config)
    assert post_deploy_items(root, config, "prod", CLEAN) == [IDENTITY]
    dirty = GitState(has_repo=True, has_origin=True, dirty=True, ahead=False)
    assert post_deploy_items(root, config, "prod", dirty) == [PUSH, IDENTITY]
    config.environments["prod"].provisioner_script_run = True
    items = post_deploy_items(root, config, "prod", dirty)
    assert items[0] == PUSH and IDENTITY not in items


def test_a_login_record_that_is_not_an_object_counts_as_no_session(deploy_layer, token_cache):
    root, config = deploy_layer
    config = oidc_ready(config)
    config.environments["prod"].oidc_registered = True
    signed_in(token_cache, config, root)
    (root / ".vecten" / "records" / "login" / "prod.json").write_text("[]")
    assert _asks_for_login(root, config)


# Service hostnames bound after the deploy wait for the next one.


def _hostname_layer(tmp_path, listed):
    import copy

    from .deploy_fixtures import STANDARD_OUTPUTS, write_outputs
    from .services_fixtures import with_services

    root = render_fixture(tmp_path, "postgres", deploy=True)
    with_services(root, {"bot": {"hostname": "bot", "routes": {"webhook": {"access": "public"}}}})
    if listed is not None:
        outputs = copy.deepcopy(STANDARD_OUTPUTS)
        outputs["hosts"]["platform"]["service_hostnames"] = listed
        write_outputs(root, outputs)
    config = oidc_ready(load_config(root))
    config.environments["prod"].oidc_registered = True
    return root, config


WAITING_HOSTNAME = Item(
    "command", "Create the DNS record for bot.data.acme.com", "vecten deploy --env prod"
)


def test_a_bound_hostname_the_outputs_do_not_list_offers_deploy_first(tmp_path):
    root, config = _hostname_layer(tmp_path, [])
    assert post_deploy_items(root, config, "prod", CLEAN)[0] == WAITING_HOSTNAME


def test_a_hostname_the_outputs_list_asks_for_nothing(tmp_path):
    root, config = _hostname_layer(tmp_path, ["bot.data.acme.com"])
    assert WAITING_HOSTNAME not in post_deploy_items(root, config, "prod", CLEAN)


def test_without_an_outputs_file_next_does_not_guess(tmp_path):
    root, config = _hostname_layer(tmp_path, None)
    assert WAITING_HOSTNAME not in post_deploy_items(root, config, "prod", CLEAN)
