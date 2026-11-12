"""`next` walks the environments of the promotion order, the first first."""

from datetime import UTC, datetime, timedelta

import pytest

from vecten.next.assemble import next_items
from vecten.next.deploy import (
    deploy_gate_items,
    deploy_prerequisite_items,
    key_handover_items,
    key_renewal_items,
)
from vecten.next.gitstate import read_git_state
from vecten.next.items import Item
from vecten.next.post_deploy import post_deploy_items
from vecten_shared.config import load_config, save_config

from .fixture_project import render_fixture
from .test_deploy_promotion import ordered
from .test_next_assemble import (
    APPLY_DEFAULT,
    clear_local_loop,
    commit_all,
    git,
    with_origin,
)
from .test_next_deploy import (
    ADMIN,
    ALERT,
    CONNECTION,
    FIRST_USER,
    GITHUB_INIT,
    LOGIN,
    OIDC,
    register_item,
    signed_in,
)


def bootstrap(env):
    return Item("command", f"Bootstrap the {env} environment", f"vecten bootstrap --env {env}")


def _for(text, env):
    return text.replace("--env prod", f"--env {env}").replace(
        "alert_webhook.prod", f"alert_webhook.{env}"
    )


def env_item(item, env):
    """`item` as `next` reports it for `env` instead of prod."""
    return Item(
        item.kind,
        _for(item.title, env),
        _for(item.resolution, env),
        answer_keys=tuple(k.replace(".prod", f".{env}") for k in item.answer_keys),
        allowed=item.allowed,
        show_first=item.show_first,
    )


DEV_GATE = Item(
    "ask",
    "Deploy to dev? This creates resources your cloud provider bills you for.",
    "vecten deploy --env dev",
    allowed=("yes", "no"),
)
PROD_AFTER_DEV = Item(
    "ask",
    "Deploy to prod? This creates resources your cloud provider bills you for. "
    "It ships the commit dev shipped last.",
    "vecten deploy --env prod",
    allowed=("yes", "no"),
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    # git without the machine's global or system config, so hooks and signing stay off
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = render_fixture(tmp_path, "postgres", deploy=True)
    clear_local_loop(root)
    return root


def in_sync(root, tmp_path):
    commit_all(root)
    with_origin(root, tmp_path)


def push(root):
    git(root, "commit", "-q", "-am", "facts")
    git(root, "push", "-q")


def facts(root, env, **values):
    config = load_config(root)
    for key, value in values.items():
        setattr(config.environments[env], key, value)
    save_config(root, config)


def clear_after_deploy(root, env):
    """Every post-deploy item of `env` answered: sign-in, first user, access, connection and
    the webhook."""
    config = load_config(root)
    config.mcp.oidc_issuer, config.mcp.oidc_client_id = "https://id.acme.com", "vecten"
    config.mcp.admins = ["ana@acme.com"]
    facts = config.environments[env]
    facts.bootstrapped = True
    facts.deployed_commit = "abc123"
    facts.oidc_registered = True
    facts.first_user_added = True
    facts.connector_access_applied = ["demo"]
    facts.connection_shown = True
    facts.alert_webhook_asked = True
    save_config(root, config)


def expire(root, env, days):
    config = load_config(root)
    when = (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config.environments[env].cloud_ids = {"key_deploy": "SCW0123", "expires_deploy": when}
    save_config(root, config)


def titles_and_resolutions(items):
    return " ".join(f"{i.title} {i.resolution}" for i in items)


def test_dev_comes_before_prod(project, tmp_path):
    ordered(project)
    commit_all(project)
    assert next_items(project) == [GITHUB_INIT, bootstrap("dev")]
    with_origin(project, tmp_path)
    items = next_items(project)
    assert items == [bootstrap("dev")]
    assert "prod" not in titles_and_resolutions(items)


def test_the_first_environments_gate_reads_as_before(project, tmp_path):
    ordered(project)
    facts(project, "dev", bootstrapped=True)
    in_sync(project, tmp_path)
    assert next_items(project) == [DEV_GATE]


def test_a_secret_is_copied_to_the_first_environment(project, tmp_path):
    ordered(project)
    facts(project, "dev", bootstrapped=True)
    (project / ".env").write_text("HUBSPOT_API_KEY=x\n")
    in_sync(project, tmp_path)
    assert next_items(project) == [
        Item(
            "command",
            "Copy HUBSPOT_API_KEY to dev",
            "vecten secret set HUBSPOT_API_KEY --env dev --from-local",
        )
    ]


def test_prod_once_dev_is_clear(project, tmp_path):
    ordered(project)
    clear_after_deploy(project, "dev")
    in_sync(project, tmp_path)
    assert next_items(project) == [bootstrap("prod")]


def test_the_gate_of_prod_says_it_ships_what_dev_shipped(project, tmp_path):
    ordered(project)
    clear_after_deploy(project, "dev")
    facts(project, "prod", bootstrapped=True)
    in_sync(project, tmp_path)
    assert next_items(project) == [PROD_AFTER_DEV]


def test_a_dev_post_deploy_item_holds_prod_back(project, tmp_path):
    ordered(project)
    clear_after_deploy(project, "dev")
    facts(project, "dev", connection_shown=False)
    in_sync(project, tmp_path)
    assert next_items(project) == [env_item(CONNECTION, "dev")]


def test_a_dev_key_leads_while_walking_prod(project, tmp_path):
    ordered(project)
    clear_after_deploy(project, "dev")
    in_sync(project, tmp_path)
    expire(project, "dev", 200)
    push(project)
    later = next_items(project)
    assert later == [bootstrap("prod")]
    expire(project, "dev", 5)
    push(project)
    items = next_items(project)
    assert items[0].resolution == "vecten bootstrap --env dev"
    assert "dev" in items[0].title
    assert items[1:] == later


def test_every_environment_with_facts_has_its_keys_renewed(project, tmp_path):
    ordered(project, order=("dev", "staging", "prod"), tfvars=("dev", "staging"))
    for env in ("prod", "staging", "dev"):
        facts(project, env, bootstrapped=True)
        expire(project, env, 5)
    in_sync(project, tmp_path)
    renewals = [i.resolution for i in next_items(project) if "Rotate" in i.title]
    assert renewals == [f"vecten bootstrap --env {env}" for env in ("dev", "staging", "prod")]


def test_the_walk_takes_dev_then_prod_to_an_empty_list(project, tmp_path, monkeypatch):
    token_cache = tmp_path / "xdg" / "vecten" / "tokens.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    ordered(project)
    in_sync(project, tmp_path)
    config = load_config(project)
    steps = {
        bootstrap("dev"): lambda c: setattr(c.environments["dev"], "bootstrapped", True),
        DEV_GATE: lambda c: setattr(c.environments["dev"], "deployed_commit", "abc123"),
        bootstrap("prod"): lambda c: setattr(c.environments["prod"], "bootstrapped", True),
        PROD_AFTER_DEV: lambda c: setattr(c.environments["prod"], "deployed_commit", "abc123"),
        OIDC: lambda c: (
            setattr(c.mcp, "oidc_issuer", "https://id.acme.com"),
            setattr(c.mcp, "oidc_client_id", "vecten"),
        ),
        ADMIN: lambda c: setattr(c.mcp, "admins", ["ana@acme.com"]),
    }
    for env in ("dev", "prod"):
        steps |= {
            register_item(config, env): lambda c, e=env: setattr(
                c.environments[e], "oidc_registered", True
            ),
            env_item(LOGIN, env): lambda c, e=env: signed_in(token_cache, c, project, e),
            env_item(FIRST_USER, env): lambda c, e=env: setattr(
                c.environments[e], "first_user_added", True
            ),
            env_item(APPLY_DEFAULT, env): lambda c, e=env: setattr(
                c.environments[e], "connector_access_applied", ["demo"]
            ),
            env_item(CONNECTION, env): lambda c, e=env: setattr(
                c.environments[e], "connection_shown", True
            ),
            env_item(ALERT, env): lambda c, e=env: setattr(
                c.environments[e], "alert_webhook_asked", True
            ),
        }
    seen = []
    while items := next_items(project):
        first = items[0]
        if first.resolution == "vecten github push":
            push(project)
            continue
        assert first not in seen, first
        seen.append(first)
        config = load_config(project)
        steps[first](config)
        save_config(project, config)
    dev = [
        bootstrap("dev"),
        DEV_GATE,
        OIDC,
        ADMIN,
        register_item(config, "dev"),
        *(env_item(i, "dev") for i in (LOGIN, FIRST_USER, APPLY_DEFAULT, CONNECTION, ALERT)),
    ]
    prod = [
        bootstrap("prod"),
        PROD_AFTER_DEV,
        register_item(config, "prod"),
        *(env_item(i, "prod") for i in (LOGIN, FIRST_USER, APPLY_DEFAULT, CONNECTION, ALERT)),
    ]
    assert seen == dev + prod


def _items_before_the_order(root):
    """What `next` reported for a deploy layer before `[deploy] promotion` existed: prod's
    keys, then prod's prerequisites, gate or post-deploy items."""
    config = load_config(root)
    now = datetime.now(UTC)
    keys = key_renewal_items(config, "prod", now) + key_handover_items(config, "prod")
    deployed = bool(config.environments.get("prod") and config.environments["prod"].deployed_commit)
    if not deployed:
        rest = deploy_prerequisite_items(root, config, "prod", read_git_state(root))
        rest = rest or deploy_gate_items(config, "prod")
    else:
        rest = post_deploy_items(root, config, "prod", read_git_state(root))
    return keys + rest


STAGES = {
    "rendered": {},
    "bootstrapped": {"bootstrapped": True},
    "deployed": {"bootstrapped": True, "deployed_commit": "abc123"},
    "connection shown": {
        "bootstrapped": True,
        "deployed_commit": "abc123",
        "first_user_added": True,
        "connection_shown": True,
    },
}


@pytest.mark.parametrize("stage", list(STAGES))
@pytest.mark.parametrize("expiring", [False, True])
@pytest.mark.parametrize("pushed", [False, True])
def test_without_an_order_next_reports_what_it_reported_before(
    project, tmp_path, stage, expiring, pushed
):
    if STAGES[stage]:
        facts(project, "prod", **STAGES[stage])
    if expiring:
        expire(project, "prod", 5)
    commit_all(project)
    if pushed:
        with_origin(project, tmp_path)
    assert load_config(project).promotion_order() == ("prod",)
    expected = _items_before_the_order(project)
    assert expected
    assert next_items(project) == expected


def _mcp_only_with_prods_script_run(tmp_path, url):
    from .test_next_deploy import render_mcp_only_deploy

    root = ordered(render_mcp_only_deploy(tmp_path, url))
    facts(root, "prod", provisioner_script_run=True)
    return root


def test_a_postgres_setup_script_run_for_prod_serves_dev(tmp_path):
    from vecten.next.existing_warehouse import existing_warehouse_items

    from .test_next_project import POSTGRES_URL

    root = _mcp_only_with_prods_script_run(tmp_path, POSTGRES_URL)
    (root / ".env").write_text("x=1\n")
    items = existing_warehouse_items(root, load_config(root), "dev")
    assert not [i for i in items if "provisioner-script" in i.resolution]


def test_a_snowflake_setup_script_names_each_environments_issuer(tmp_path):
    from vecten.next.existing_warehouse import existing_warehouse_items

    from .test_next_project import SNOWFLAKE_URL

    root = _mcp_only_with_prods_script_run(tmp_path, SNOWFLAKE_URL)
    items = existing_warehouse_items(root, load_config(root), "dev")
    assert [i.resolution for i in items] == ["vecten mcp provisioner-script --env dev"]
    assert existing_warehouse_items(root, load_config(root), "prod") == []


def test_without_a_deploy_layer_a_deployed_prod_waits_for_the_deploy_questions(project):
    """As before the order existed: the deploy questions come before any environment."""
    clear_after_deploy(project, "prod")
    facts(project, "prod", connection_shown=False)
    config = load_config(project)
    config.deploy = None
    save_config(project, config)
    assert [i.answer_keys for i in next_items(project)] == [("cloud",)]
