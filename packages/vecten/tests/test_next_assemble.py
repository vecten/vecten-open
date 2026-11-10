import socket
import subprocess

import pytest
import yaml

from vecten.connectors.names import run_record_name
from vecten.intake.answers import ANSWER_KEYS, ANSWERS_FILE
from vecten.next.assemble import next_items
from vecten.next.gitstate import GitState, read_git_state
from vecten.next.items import Item
from vecten.password_links import added, issued
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import AccessDecl, load_config, save_config
from vecten_shared.records import remove_record, write_record

from .fixture_project import render_fixture
from .test_next_deploy import (
    ADMIN,
    ALERT,
    BOOTSTRAP,
    CLOUD,
    CONNECTION,
    DEPLOY_ANSWERS,
    DOMAIN,
    FIRST_USER,
    GATE,
    GITHUB_INIT,
    LOGIN,
    OIDC,
    PROVISIONER_SECRETS,
    PUSH,
    register_item,
    render_mcp_only_deploy,
    signed_in,
)
from .test_next_project import (
    LOCAL_PROVISIONER_ENV,
    POSTGRES_URL,
    PROVISIONER_SCRIPT,
    SNOWFLAKE_URL,
    green_build,
    render_mcp_only,
    validated,
)

UP = Item("command", "Start the local stack", "vecten up")
APPLY_DEFAULT = Item(
    "command",
    "Grant default access to raw_demo",
    "vecten group add default --env prod"
    " && vecten grant --schema raw_demo --group default --env prod",
)
INTAKE_FIRST = ANSWER_KEYS["has_warehouse"].question


@pytest.fixture
def isolated_git(monkeypatch):
    """Run git without the machine's global or system config, so hooks and signing stay off."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def commit_all(root):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")


def with_origin(root, tmp_path):
    """Give `root` an origin it is in sync with: a local bare repository, no network."""
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "-u", "origin", "main")


def write_answers(root, answers):
    (root / ANSWERS_FILE).write_text(yaml.safe_dump(answers))


def clear_local_loop(root):
    """Stage every local-loop record and answer on a rendered fixture project."""
    write_record(root, "up", {"profiles": []})
    config = load_config(root)
    config.connector("demo").access = AccessDecl(groups=["default"])
    save_config(root, config)
    write_record(root, run_record_name("demo"), {"ok": True})
    green_build(root)
    validated(root)


def clear_mcp_only_local_loop(root, tmp_path):
    """Stage the stack record, the local credentials and a validated semantic model."""
    write_record(root, "up", {"profiles": []})
    (root / ".env").write_text("WAREHOUSE_ADMIN_PASSWORD=x\n" + LOCAL_PROVISIONER_ENV[POSTGRES_URL])
    fixture = render_fixture(tmp_path / "platform", "postgres")
    for name in ("datasets.json", "metrics.json"):
        (root / "osi" / name).write_text((fixture / "osi" / name).read_text())
    validated(root)


def resolutions(items):
    return [i.resolution for i in items]


def test_outside_a_project_runs_the_intake(tmp_path):
    items = next_items(tmp_path)
    assert [i.title for i in items] == [INTAKE_FIRST]


def test_fresh_render_reports_up_then_the_wanted_sources(tmp_path):
    root = tmp_path / "proj"
    answers = {"name": "acme", "users": 3, "sources": ["hubspot", "pipedrive"]}
    render_project(root, ScaffoldParams.from_answers({**answers, "warehouse": "postgres"}, {}))
    items = next_items(root)
    assert items[0] == UP
    assert items[1:3] == [
        Item("skill", "Connect hubspot", "connector"),
        Item("skill", "Connect pipedrive", "connector"),
    ]
    assert not any("github" in (i.title + i.resolution).lower() for i in items)


def test_fresh_fixture_reports_no_deploy_question(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    items = next_items(root)
    assert items[0] == UP
    assert CLOUD not in items and GATE not in items


def test_local_loop_clear_asks_the_cloud_first(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    clear_local_loop(root)
    assert next_items(root) == [CLOUD]


def test_wanted_source_suppresses_the_deploy_questions(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    clear_local_loop(root)
    config = load_config(root)
    config.wanted = [*config.wanted, "hubspot"]
    save_config(root, config)
    items = next_items(root)
    assert items == [Item("skill", "Connect hubspot", "connector")]


def test_next_from_a_subdirectory_reports_the_project(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    clear_local_loop(root)
    assert next_items(root / "dbt" / "models" / "core") == next_items(root) == [CLOUD]


def test_next_ignores_unknown_answer_keys(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    clear_local_loop(root)
    write_answers(root, {"colour": "blue", "cloud": "aws", "access.demo": "sales"})
    assert next_items(root) == [DOMAIN]
    write_answers(root, {"colour": "blue", **DEPLOY_ANSWERS})
    assert resolutions(next_items(root)) == ["vecten init --deploy"]


def test_next_does_not_write_the_config(tmp_path):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    clear_local_loop(root)
    before = (root / "vecten.toml").read_text()
    next_items(root)
    assert (root / "vecten.toml").read_text() == before
    assert "prod" not in load_config(root).environments


def test_deploy_layer_rendered_hides_init_deploy(tmp_path, isolated_git):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    clear_local_loop(root)
    write_answers(root, DEPLOY_ANSWERS)
    items = next_items(root)
    assert "vecten init --deploy" not in resolutions(items)
    assert not any(i.answer_keys and i.answer_keys[0] in DEPLOY_ANSWERS for i in items)
    assert items[0] == GITHUB_INIT


def test_prerequisites_without_origin_name_github_init(tmp_path, isolated_git):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    clear_local_loop(root)
    commit_all(root)
    assert next_items(root) == [GITHUB_INIT, BOOTSTRAP]


def test_pushed_and_bootstrapped_reports_the_gate(tmp_path, isolated_git):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    clear_local_loop(root)
    config = load_config(root)
    config.environments["prod"].bootstrapped = True
    save_config(root, config)
    commit_all(root)
    with_origin(root, tmp_path)
    assert next_items(root) == [GATE]
    (root / "README.md").write_text("changed\n")
    assert next_items(root) == [PUSH]


def test_read_git_state(tmp_path, isolated_git):
    root = render_fixture(tmp_path, "postgres")
    assert read_git_state(root) == GitState(False, False, False, False)
    commit_all(root)
    assert read_git_state(root) == GitState(True, False, False, False)
    (root / "README.md").write_text("changed\n")
    assert read_git_state(root).dirty is True
    git(root, "commit", "-q", "-am", "change")
    with_origin(root, tmp_path)
    assert read_git_state(root) == GitState(True, True, False, False)
    (root / "README.md").write_text("again\n")
    git(root, "commit", "-q", "-am", "again")
    assert read_git_state(root) == GitState(True, True, False, True)
    git(root, "branch", "--unset-upstream")
    assert read_git_state(root).ahead is True


def test_project_inside_another_repository_has_no_repository(tmp_path, isolated_git):
    parent = tmp_path / "monorepo"
    root = render_fixture(parent, "postgres", deploy=True)
    clear_local_loop(root)
    commit_all(parent)
    assert read_git_state(root) == GitState(False, False, False, False)
    assert next_items(root)[0] == GITHUB_INIT


def test_read_git_state_without_git_on_path(tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert read_git_state(root) == GitState(False, False, False, False)


def test_next_never_calls_anything_but_git(tmp_path, monkeypatch, isolated_git):
    root = render_fixture(tmp_path / "a", "postgres", deploy=True)
    clear_local_loop(root)
    commit_all(root)
    calls = []
    real_run = subprocess.run

    def only_git(*args, **kwargs):
        argv = args[0] if args else kwargs["args"]
        if argv[0] != "git":
            raise AssertionError(f"next ran {argv!r}")
        calls.append(argv)
        return real_run(*args, **kwargs)

    def no_socket(*args, **kwargs):
        raise AssertionError("next opened a socket")

    monkeypatch.setattr(subprocess, "run", only_git)
    monkeypatch.setattr(socket, "socket", no_socket)
    assert next_items(root)[0] == GITHUB_INIT
    assert calls and all(c[0] == "git" for c in calls)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert [i.title for i in next_items(empty)] == [INTAKE_FIRST]


def test_post_deploy_items_appear_once_each_and_end_empty(tmp_path, monkeypatch):
    token_cache = tmp_path / "xdg" / "vecten" / "tokens.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = render_fixture(tmp_path, "postgres", deploy=True)
    clear_local_loop(root)
    config = load_config(root)
    facts = config.environments["prod"]
    facts.bootstrapped = True
    facts.deployed_commit = "abc123"
    save_config(root, config)
    register = register_item(config)

    def resolve(item, config):
        facts = config.environments["prod"]
        if item == OIDC:
            config.mcp.oidc_issuer, config.mcp.oidc_client_id = "https://id.acme.com", "vecten"
        elif item == ADMIN:
            config.mcp.admins = ["ana@acme.com"]
        elif item == register:
            facts.oidc_registered = True
        elif item == LOGIN:
            signed_in(token_cache, config, root)
        elif item == FIRST_USER:
            facts.first_user_added = True
        elif item == APPLY_DEFAULT:
            facts.connector_access_applied = ["demo"]
        elif item == CONNECTION:
            facts.connection_shown = True
        elif item == ALERT:
            facts.alert_webhook_asked = True
        else:
            raise AssertionError(item)

    seen = []
    while items := next_items(root):
        assert items[0] not in seen
        seen.append(items[0])
        config = load_config(root)
        resolve(items[0], config)
        save_config(root, config)
    assert seen == [
        OIDC,
        ADMIN,
        register,
        LOGIN,
        FIRST_USER,
        APPLY_DEFAULT,
        CONNECTION,
        ALERT,
    ]
    assert next_items(root) == []


def test_post_deploy_items_under_password_sign_in_end_empty(tmp_path, monkeypatch):
    token_cache = tmp_path / "xdg" / "vecten" / "tokens.json"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = render_fixture(tmp_path, "postgres", deploy=True, domain="acme.test")
    clear_local_loop(root)
    config = load_config(root)
    config.mcp.auth = "password"
    facts = config.environments["prod"]
    facts.bootstrapped = True
    facts.deployed_commit = "abc123"
    save_config(root, config)
    link = Item(
        "command",
        "Give grace@acme.test a password link",
        "vecten user password-link p-grace --env prod",
    )
    admin_link = Item(
        "command", "Give the first admin a password link", "vecten mcp admin-link --env prod"
    )

    def resolve(item, config):
        facts = config.environments["prod"]
        if item == ADMIN:
            config.mcp.admins = ["ana@acme.com"]
        elif item == admin_link:
            facts.admin_link_issued = True
        elif item == LOGIN:
            signed_in(token_cache, config, root)
        elif item == FIRST_USER:
            facts.first_user_added = True
            added(root, "prod", "p-grace", "grace@acme.test")
        elif item == link:
            issued(root, "prod", "p-grace")
        elif item == APPLY_DEFAULT:
            facts.connector_access_applied = ["demo"]
        elif item == CONNECTION:
            facts.connection_shown = True
        elif item == ALERT:
            facts.alert_webhook_asked = True
        else:
            raise AssertionError(item)

    seen = []
    while items := next_items(root):
        assert items[0] not in seen
        seen.append(items[0])
        config = load_config(root)
        resolve(items[0], config)
        save_config(root, config)
    assert seen == [
        ADMIN,
        admin_link,
        LOGIN,
        FIRST_USER,
        link,
        APPLY_DEFAULT,
        CONNECTION,
        ALERT,
    ]
    assert next_items(root) == []


def test_mcp_only_local_loop_asks_for_the_provisioner_script_alone(tmp_path):
    root = render_mcp_only(tmp_path)
    clear_mcp_only_local_loop(root, tmp_path)
    assert next_items(root) == [PROVISIONER_SCRIPT]
    config = load_config(root)
    config.environments["prod"].provisioner_script_run = True
    save_config(root, config)
    assert next_items(root) == [CLOUD]


def test_mcp_only_provisioner_secrets_come_after_bootstrap(tmp_path, isolated_git):
    root = render_mcp_only_deploy(tmp_path, POSTGRES_URL)
    clear_mcp_only_local_loop(root, tmp_path)
    config = load_config(root)
    config.environments["prod"].provisioner_script_run = True
    save_config(root, config)
    store = PROVISIONER_SECRETS[POSTGRES_URL]
    assert next_items(root) == [GITHUB_INIT, BOOTSTRAP, store]


def test_mcp_only_local_loop_puts_the_stack_last(tmp_path):
    root = render_mcp_only(tmp_path)
    clear_mcp_only_local_loop(root, tmp_path)
    remove_record(root, "up")
    (root / ".env").write_text("WAREHOUSE_ADMIN_PASSWORD=x\n")
    script = PROVISIONER_SCRIPT.title
    store = "Store the provisioner credentials locally"
    assert [i.title for i in next_items(root)] == [store, script, UP.title]
    (root / ".env").write_text("WAREHOUSE_ADMIN_PASSWORD=x\n" + LOCAL_PROVISIONER_ENV[POSTGRES_URL])
    assert [i.title for i in next_items(root)] == [script, UP.title]


def test_an_existing_snowflake_warehouse_goes_from_the_model_to_the_deploy_questions(tmp_path):
    root = render_mcp_only(tmp_path, SNOWFLAKE_URL)
    clear_mcp_only_local_loop(root, tmp_path)
    remove_record(root, "up")
    (root / ".env").write_text(
        "WAREHOUSE_ADMIN_PASSWORD=x\n" + LOCAL_PROVISIONER_ENV[SNOWFLAKE_URL]
    )
    assert next_items(root) == [CLOUD]


def test_admin_password_reaches_the_deploy_ask(tmp_path, isolated_git):
    root = render_mcp_only_deploy(tmp_path, POSTGRES_URL)
    clear_mcp_only_local_loop(root, tmp_path)
    write_record(root, "provisioner-prod", {"confirmed": True})
    config = load_config(root)
    config.environments["prod"].bootstrapped = True
    config.environments["prod"].provisioner_secrets_set = True
    save_config(root, config)
    commit_all(root)
    with_origin(root, tmp_path)
    assert next_items(root) == [GATE]


def test_deployed_project_pushes_its_changes_first(tmp_path, isolated_git):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    clear_local_loop(root)
    config = load_config(root)
    config.environments["prod"].bootstrapped = True
    config.environments["prod"].deployed_commit = "abc123"
    save_config(root, config)
    commit_all(root)
    with_origin(root, tmp_path)
    assert next_items(root)[0] == OIDC
    config = load_config(root)
    config.mcp.auth = "password"
    save_config(root, config)
    assert next_items(root)[:2] == [PUSH, ADMIN]
    git(root, "commit", "-q", "-am", "password sign-in")
    assert next_items(root)[:2] == [PUSH, ADMIN]
    git(root, "push", "-q")
    assert next_items(root)[0] == ADMIN


def _expiring(root, days):
    from datetime import UTC, datetime, timedelta

    config = load_config(root)
    facts = config.environments["prod"]
    facts.bootstrapped = True
    facts.deployed_commit = "abc123"
    when = (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    facts.cloud_ids = {"key_deploy": "SCW0123", "expires_deploy": when}
    save_config(root, config)


def test_a_key_that_expires_soon_leads_with_bootstrap(tmp_path):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    _expiring(root, 200)
    later = next_items(root)
    _expiring(root, 5)
    items = next_items(root)
    assert items[0].resolution == "vecten bootstrap --env prod"
    assert "deploy" in items[0].title
    assert items[1:] == later
