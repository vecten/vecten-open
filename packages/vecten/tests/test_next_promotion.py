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
    UP,
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
    from vecten_shared.identity_setup import ACCESS_VERSION

    from .test_next_deploy import render_mcp_only_deploy

    root = ordered(render_mcp_only_deploy(tmp_path, url))
    # The current release's confirmation records the Snowflake procedures' version.
    installed = {"access_version": ACCESS_VERSION} if url.startswith("snowflake") else {}
    facts(root, "prod", provisioner_script_run=True, **installed)
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


# A later environment's ship once its Snowflake identity is confirmed


def _snowflake_dev_prod(tmp_path):
    from .test_deploy_snowflake import render_snowflake

    root = ordered(render_snowflake(tmp_path / "snow"))
    for env in ("dev", "prod"):
        facts(root, env, bootstrapped=True, deployed_commit="abc123")
    config = load_config(root)
    config.mcp.oidc_issuer, config.mcp.oidc_client_id = "https://id.acme.com", "vecten"
    config.mcp.admins = ["ana@acme.com"]
    save_config(root, config)
    return root


def _post_deploy(root, env):
    from vecten.next.gitstate import GitState

    clean = GitState(has_repo=True, has_origin=True, dirty=False, ahead=False)
    return post_deploy_items(root, load_config(root), env, clean)


def test_another_checkout_waits_for_the_ship_with_its_mcp_settings(tmp_path, monkeypatch):
    from shutil import copyfile, copytree, ignore_patterns

    from vecten.deploy.promotion import mcp_ship_receipt
    from vecten.deploy.run import promotion_status, recorded_ship
    from vecten.github import gh
    from vecten.intake.facts import set_mcp_fact
    from vecten.mcp_onboarding import setup_script

    from .deploy_fixtures import COMMIT, FakeGh

    root = _snowflake_dev_prod(tmp_path)
    data = setup_script(root, "prod", confirm=True)
    assert data["commands"] == ["vecten github push", "vecten promote --env prod"]
    other = tmp_path / "second-checkout"
    copytree(root, other, ignore=ignore_patterns(".vecten"))
    github = FakeGh(history=[COMMIT])
    monkeypatch.setattr(gh, "get_gh", lambda: github)

    def commands():
        return [item.resolution for item in _post_deploy(other, "prod")]

    def ship():
        receipt = mcp_ship_receipt(load_config(root), "prod")
        recorded_ship(
            github,
            root,
            "acme/data",
            "prod",
            COMMIT,
            lambda: ["mcp"],
            required=True,
            receipt=receipt,
        )

    assert commands() == ["vecten promote --status"]
    promotion_status(other, gh=github)
    assert commands() == ["vecten promote --env prod"]
    ship()
    promotion_status(other, gh=github)
    assert any(cmd.startswith("vecten user add") for cmd in commands())
    set_mcp_fact(other, "mcp.admins", "other@example.com")
    assert commands() == ["vecten promote --env prod"]
    ship()  # The old promoted commit still carries the previous admins.
    promotion_status(other, gh=github)
    assert commands() == ["vecten promote --env prod"]
    copyfile(other / "vecten.toml", root / "vecten.toml")
    ship()
    promotion_status(other, gh=github)
    assert any(cmd.startswith("vecten user add") for cmd in commands())
    github.failing = ("api",)
    assert any(cmd.startswith("vecten user add") for cmd in commands())
    github.failing = ()
    github.history = [f"{i:040x}" for i in range(201)] + [COMMIT]
    promotion_status(other, gh=github)
    assert any(cmd.startswith("vecten user add") for cmd in commands())
    github.history = [COMMIT]
    copyfile(other / "vecten.toml", root / "vecten.toml")
    set_mcp_fact(root, "mcp.admins", "old@example.com")
    ship()
    promotion_status(other, gh=github)
    assert commands() == ["vecten promote --env prod"]


def test_existing_confirmation_without_a_checkout_record_uses_legacy_ship_evidence(
    tmp_path, monkeypatch
):
    from vecten.deploy import run

    from .deploy_fixtures import COMMIT, FakeGh, FakeGit

    root = _snowflake_dev_prod(tmp_path)
    facts(root, "prod", provisioner_script_run=True)
    github = FakeGh(history=[COMMIT])
    assert [i.resolution for i in _post_deploy(root, "prod")] == ["vecten promote --status"]
    run.promotion_status(root, gh=github)
    assert [i.resolution for i in _post_deploy(root, "prod")] == ["vecten promote --env prod"]
    github.statuses[COMMIT] = [("vecten/ship/prod", "success", "2026-01-01T00:00:00Z")]
    monkeypatch.setattr(run, "get_git", lambda: FakeGit(shown=root))
    run.promotion_status(root, gh=github)
    github.failing = ("api",)
    assert any(i.resolution.startswith("vecten user add") for i in _post_deploy(root, "prod"))


def test_completed_legacy_onboarding_outside_recent_history_keeps_offline_guidance(tmp_path):
    from vecten.deploy.run import promotion_status

    from .deploy_fixtures import COMMIT, FakeGh

    root = _snowflake_dev_prod(tmp_path)
    facts(root, "prod", provisioner_script_run=True, first_user_added=True)
    github = FakeGh(history=[f"{i:040x}" for i in range(201)] + [COMMIT])
    github.statuses[COMMIT] = [("vecten/ship/prod", "success", "2026-01-01T00:00:00Z")]
    promotion_status(root, gh=github)
    resolutions = [item.resolution for item in _post_deploy(root, "prod")]
    assert "vecten mcp connection --env prod" in resolutions
    assert not any("promote" in cmd for cmd in resolutions)


def test_the_first_environment_is_shipped_by_the_push_that_carries_its_identity(tmp_path):
    from vecten.deploy.snowflake import owed_ship_record
    from vecten.mcp_onboarding import setup_script
    from vecten_shared.records import read_record

    root = _snowflake_dev_prod(tmp_path)
    assert setup_script(root, "dev", confirm=True)["commands"] == ["vecten github push"]
    assert read_record(root, owed_ship_record("dev")) is None
    resolutions = [i.resolution for i in _post_deploy(root, "dev")]
    assert not [r for r in resolutions if r.startswith("vecten promote")]
    assert any(r.startswith("vecten user add") for r in resolutions)


def test_the_script_for_a_later_environment_lists_its_promotion(tmp_path, monkeypatch):
    from vecten import mcp_onboarding

    root = _snowflake_dev_prod(tmp_path)
    monkeypatch.setattr(mcp_onboarding, "_platform_public_key", lambda root, env: "AAAA")
    assert mcp_onboarding.setup_script(root, "prod")["commands"] == [
        "vecten mcp provisioner-script --env prod --confirm-run",
        "vecten github push",
        "vecten promote --env prod",
    ]


def test_no_ship_is_owed_where_the_deploy_has_not_run_or_the_identity_was_confirmed(tmp_path):
    from vecten.deploy.snowflake import owed_ship_record
    from vecten.mcp_onboarding import setup_script
    from vecten_shared.records import read_record

    root = _snowflake_dev_prod(tmp_path)
    facts(root, "prod", deployed_commit=None)
    setup_script(root, "prod", confirm=True)
    assert read_record(root, owed_ship_record("prod")) is None
    facts(root, "prod", deployed_commit="abc123")
    setup_script(root, "prod", confirm=True)
    assert read_record(root, owed_ship_record("prod")) is None


# A deployed environment's items beside an unfinished local loop


def _unfinish_local_loop(root):
    from vecten_shared.records import remove_record

    remove_record(root, "up")


def test_a_deployed_environment_reports_its_items_after_the_local_loop(project):
    facts(project, "prod", bootstrapped=True, deployed_commit="abc123")
    _unfinish_local_loop(project)
    items = next_items(project)
    assert items[0] == UP
    assert items[1:] == [OIDC, ADMIN, FIRST_USER, APPLY_DEFAULT, CONNECTION, ALERT]


def test_an_undeployed_environment_waits_for_the_local_loop(project, tmp_path):
    ordered(project)
    clear_after_deploy(project, "dev")
    in_sync(project, tmp_path)
    assert next_items(project) == [bootstrap("prod")]
    _unfinish_local_loop(project)
    assert next_items(project) == [UP]


def test_the_local_loop_and_the_walk_report_a_shared_item_once(tmp_path):
    from vecten_shared.records import write_record

    from .test_next_deploy import deployed, render_mcp_only_deploy, rerun_item
    from .test_next_project import SNOWFLAKE_URL

    root = render_mcp_only_deploy(tmp_path, SNOWFLAKE_URL)
    config = deployed(load_config(root))
    config.environments["prod"].provisioner_script_run = True
    save_config(root, config)
    write_record(root, "provisioner-prod", {"confirmed": True})
    items = next_items(root)
    assert items.count(rerun_item()) == 1
