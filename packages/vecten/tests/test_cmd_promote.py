"""`vecten promote`: ship a later environment a commit the environment before it shipped."""

import json

import pytest

from vecten.deploy import github_env
from vecten.github import gh as gh_mod

from .deploy_fixtures import COMMIT, FakeGh, FakeGit, render_aws, set_facts
from .test_deploy_promotion import ordered

HEAD = "b71d004b71d004b71d004b71d004b71d004b71d0"
SHIPPED = "a3f9c2e0a3f9c2e0a3f9c2e0a3f9c2e0a3f9c2e0"
OLDER = "5c20e8105c20e8105c20e8105c20e8105c20e810"
DEV_OK = ("vecten/ship/dev", "success")
PROD_OK = ("vecten/ship/prod", "success")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = ordered(render_aws(tmp_path / "proj"))
    for env in ("dev", "prod"):
        set_facts(root, env, bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    return root


def use(monkeypatch, gh: FakeGh, git: FakeGit | None = None) -> FakeGh:
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    if git is not None:
        monkeypatch.setattr(gh_mod, "get_git", lambda: git)
    return gh


def doc(result):
    return json.loads(result.stdout)


def resolving(commit: str) -> FakeGit:
    return FakeGit(head=HEAD, answers={("rev-parse", "--verify"): (0, commit + "\n")})


def test_promote_ships_the_newest_commit_dev_shipped(invoke, project, monkeypatch):
    gh = use(
        monkeypatch,
        FakeGh(history=[HEAD, SHIPPED, OLDER], statuses={SHIPPED: [DEV_OK], OLDER: [DEV_OK]}),
    )
    r = invoke("promote", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    [dispatch] = gh.named("workflow", "run")
    assert dispatch == [
        "workflow",
        "run",
        "deploy-platform.yml",
        "--ref",
        "main",
        "-f",
        "environment=prod",
        "-f",
        f"commit={SHIPPED}",
        "--repo",
        "acme/data",
    ]
    data = doc(r)
    assert data["env"] == "prod" and data["promotes_from"] == "dev"
    assert data["commit"] == SHIPPED
    assert data["url"] == "https://github.com/acme/data/actions/runs/42"
    text = invoke("promote", "--env", "prod")
    assert text.exit_code == 0
    assert SHIPPED[:8] in text.stdout and "dev" in text.stdout and "prod" in text.stdout


def test_promote_with_commit_rolls_production_back(invoke, project, monkeypatch):
    gh = use(
        monkeypatch,
        FakeGh(history=[HEAD, SHIPPED, OLDER], statuses={SHIPPED: [DEV_OK], OLDER: [DEV_OK]}),
        resolving(OLDER),
    )
    r = invoke("promote", "--env", "prod", "--commit", OLDER[:7], "--json")
    assert r.exit_code == 0, r.stdout
    [dispatch] = gh.named("workflow", "run")
    assert f"commit={OLDER}" in dispatch and doc(r)["commit"] == OLDER


def test_promote_refuses_a_commit_dev_did_not_ship(invoke, project, monkeypatch):
    gh = use(
        monkeypatch, FakeGh(statuses={OLDER: [("vecten/ship/dev", "failure")]}), resolving(OLDER)
    )
    r = invoke("promote", "--env", "prod", "--commit", OLDER, "--json")
    assert r.exit_code == 4, r.stdout
    assert OLDER[:8] in doc(r)["error"] and "dev" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_promote_refuses_a_commit_off_the_default_branch(invoke, project, monkeypatch):
    git = resolving(OLDER)
    git.answers[("merge-base",)] = (1, "")
    gh = use(monkeypatch, FakeGh(statuses={OLDER: [DEV_OK]}), git)
    r = invoke("promote", "--env", "prod", "--commit", OLDER, "--json")
    assert r.exit_code == 4, r.stdout
    assert "main" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_promote_before_dev_shipped_anything_names_dev(invoke, project, monkeypatch):
    gh = use(monkeypatch, FakeGh(history=[HEAD]))
    r = invoke("promote", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    assert "dev" in doc(r)["error"] and "--commit" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_promote_of_the_first_environment_says_a_push_ships_it(invoke, project, monkeypatch):
    gh = use(monkeypatch, FakeGh())
    r = invoke("promote", "--env", "dev", "--json")
    assert r.exit_code == 4, r.stdout
    assert "push" in doc(r)["error"] and "dev" in doc(r)["error"]
    assert gh.calls == []


def test_promote_of_an_environment_outside_the_order_is_refused(invoke, project, monkeypatch):
    gh = use(monkeypatch, FakeGh())
    r = invoke("promote", "--env", "sandbox", "--json")
    assert r.exit_code == 4, r.stdout
    assert "deploy.promotion" in doc(r)["error"] and "sandbox" in doc(r)["error"]
    assert gh.calls == []


def test_promote_of_an_environment_never_deployed_names_deploy(invoke, project, monkeypatch):
    set_facts(project, "prod", deployed_commit=None)
    gh = use(monkeypatch, FakeGh(history=[SHIPPED], statuses={SHIPPED: [DEV_OK]}))
    r = invoke("promote", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "vecten deploy --env prod" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_a_failed_promotion_run_exits_5_with_its_url(invoke, project, monkeypatch):
    use(monkeypatch, FakeGh(watch_code=1, history=[SHIPPED], statuses={SHIPPED: [DEV_OK]}))
    r = invoke("promote", "--env", "prod", "--json")
    assert r.exit_code == 5, r.stdout
    assert "actions/runs/42" in doc(r)["error"]


def test_promote_without_an_order_refuses_prod_as_the_first_environment(
    invoke, tmp_path, monkeypatch
):
    root = render_aws(tmp_path / "plain")
    set_facts(root, "prod", bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    monkeypatch.chdir(root)
    gh = use(monkeypatch, FakeGh())
    r = invoke("promote", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "push" in doc(r)["error"]
    assert gh.calls == []


def test_promote_needs_env_or_status(invoke, project, monkeypatch):
    use(monkeypatch, FakeGh())
    assert invoke("promote", "--json").exit_code == 2
    both = invoke("promote", "--status", "--env", "prod", "--json")
    assert both.exit_code == 2 and "--status" in doc(both)["error"]


# promote --status


def test_status_lists_each_environment_with_what_it_shipped_last(invoke, project, monkeypatch):
    use(
        monkeypatch,
        FakeGh(
            history=[HEAD, SHIPPED, OLDER],
            statuses={HEAD: [DEV_OK], SHIPPED: [DEV_OK, PROD_OK], OLDER: [PROD_OK]},
        ),
    )
    r = invoke("promote", "--status", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["environments"] == [
        {"env": "dev", "promotes_from": None, "commit": HEAD, "behind": False},
        {"env": "prod", "promotes_from": "dev", "commit": SHIPPED, "behind": True},
    ]
    assert doc(r)["order"] == ["dev", "prod"]
    text = invoke("promote", "--status")
    assert text.exit_code == 0
    assert f"prod {SHIPPED[:8]}, behind dev" in text.stdout


def test_status_of_an_environment_level_with_the_one_before_it(invoke, project, monkeypatch):
    use(monkeypatch, FakeGh(history=[HEAD, SHIPPED], statuses={SHIPPED: [DEV_OK, PROD_OK]}))
    r = invoke("promote", "--status", "--json")
    assert [e["behind"] for e in doc(r)["environments"]] == [False, False]


def test_status_of_an_environment_that_shipped_nothing(invoke, project, monkeypatch):
    use(monkeypatch, FakeGh(history=[HEAD, SHIPPED], statuses={SHIPPED: [DEV_OK]}))
    r = invoke("promote", "--status", "--json")
    prod = doc(r)["environments"][1]
    assert prod == {"env": "prod", "promotes_from": "dev", "commit": None, "behind": True}


def test_status_without_an_order_lists_prod(invoke, tmp_path, monkeypatch):
    root = render_aws(tmp_path / "plain")
    monkeypatch.chdir(root)
    use(monkeypatch, FakeGh(history=[HEAD], statuses={HEAD: [PROD_OK]}))
    r = invoke("promote", "--status", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["environments"] == [
        {"env": "prod", "promotes_from": None, "commit": HEAD, "behind": False}
    ]


def test_after_a_rollback_by_commit_promote_and_status_follow_the_later_ship(
    invoke, project, monkeypatch
):
    # dev shipped SHIPPED, then was rolled back to OLDER: OLDER's success is the later one.
    gh = use(
        monkeypatch,
        FakeGh(
            history=[HEAD, SHIPPED, OLDER],
            statuses={
                SHIPPED: [("vecten/ship/dev", "success", "2026-03-01T10:00:00Z")],
                OLDER: [
                    ("vecten/ship/dev", "success", "2026-02-01T10:00:00Z"),
                    ("vecten/ship/dev", "success", "2026-03-02T10:00:00Z"),
                    ("vecten/ship/prod", "success", "2026-02-02T10:00:00Z"),
                ],
            },
        ),
    )
    status = invoke("promote", "--status", "--json")
    assert status.exit_code == 0, status.stdout
    assert [(e["env"], e["commit"], e["behind"]) for e in doc(status)["environments"]] == [
        ("dev", OLDER, False),
        ("prod", OLDER, False),
    ]
    r = invoke("promote", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    [dispatch] = gh.named("workflow", "run")
    assert f"commit={OLDER}" in dispatch
