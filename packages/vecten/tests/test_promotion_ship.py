"""Shipping and deploying a given commit under a promotion order.

Inside Actions the commands run against a real git repository with a local `origin`, the
mocked cloud and a fake GitHub: content, tfvars and Terraform roots come from the promoted
commit, the environment facts from the default branch's head.
"""

import json
import subprocess
from pathlib import Path

import pytest

from vecten import git_extensions
from vecten.deploy import github_env
from vecten.deploy import run as run_mod
from vecten.github import gh as gh_mod
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import _REGISTRY

from .deploy_fixtures import COMMIT, FakeGh, FakeGit, render_aws, set_facts
from .test_deploy_promotion import ordered

MOCK_DIR = Path(__file__).parent / "fixtures" / "mock_cloud"
MARKER = Path("evals") / "promotion-marker.txt"
DEV_OK = ("vecten/ship/dev", "success")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


class Repo:
    """`a` was made before prod was deployed; `b`, the default branch's head, records prod's
    deploy and changes the content and prod's tfvars; `feature` exists on a branch only."""

    root: Path
    cloud: Path
    a: str
    b: str
    feature: str


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Repo:
    root = ordered(render_aws(tmp_path / "proj"))
    set_facts(root, "dev", bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    (root / MARKER).parent.mkdir(exist_ok=True)
    (root / MARKER).write_text("a\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Promotion")
    git(root, "config", "user.email", "promotion@acme.test")
    a = commit_all(root, "Content a")
    (root / MARKER).write_text("b\n")
    prod_tfvars = root / "deploy" / "envs" / "prod.tfvars"
    prod_tfvars.write_text(prod_tfvars.read_text() + "# written after a\n")
    set_facts(root, "prod", bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    b = commit_all(root, "Content b and the prod deploy")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "main")
    git(root, "checkout", "-q", "-b", "feature")
    (root / MARKER).write_text("feature\n")
    feature = commit_all(root, "Unreviewed")
    git(root, "checkout", "-q", "main")

    cloud = tmp_path / "cloud"
    monkeypatch.syspath_prepend(str(MOCK_DIR))
    monkeypatch.setenv("VECTEN_CONTROL_PLANE", "vecten_mock_cloud:MockCloud")
    monkeypatch.setenv("VECTEN_MOCK_CLOUD_DIR", str(cloud))
    monkeypatch.setattr("vecten_shared.provider._REGISTRY", dict(_REGISTRY))
    from vecten_mock_cloud import MockCloud

    for env in ("dev", "prod"):
        MockCloud(root, env).store_outputs(MockCloud.OUTPUTS)
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/data")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("VECTEN_GIT_RUNNER", raising=False)
    found = Repo()
    found.root, found.cloud, found.a, found.b, found.feature = root, cloud, a, b, feature
    return found


def use_gh(monkeypatch, gh: FakeGh) -> FakeGh:
    monkeypatch.setattr(gh_mod, "get_gh", lambda: gh)
    return gh


def doc(result):
    return json.loads(result.stdout)


def released(cloud: Path, host: str) -> Path | None:
    from vecten_mock_cloud import MockCloud

    if not (cloud / "hosts" / host / "current").is_file():
        return None
    return MockCloud.release(cloud, host)


# Inside Actions: `ship --env <env> --commit <sha>`


def test_a_promoted_commit_ships_its_content_with_the_heads_facts(invoke, repo, monkeypatch):
    gh = use_gh(monkeypatch, FakeGh(history=[repo.b, repo.a], statuses={repo.a: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--commit", repo.a, "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r)["commit"] == repo.a
    for host in ("mcp", "platform"):
        release = released(repo.cloud, host)
        assert release.name == repo.a
        assert (release / MARKER).read_text() == "a\n"
        # Commit a predates prod's deploy; the head's facts say prod is deployed.
        shipped = load_config(release)
        assert shipped.environments["prod"].deployed_commit == COMMIT
    assert gh.posted_statuses() == [
        (repo.a, "vecten/ship/prod", "pending"),
        (repo.a, "vecten/ship/prod", "success"),
    ]
    assert git(repo.root, "worktree", "list").count("\n") == 0
    assert "vecten.toml" not in git(repo.root, "status", "--porcelain")


def test_an_abbreviated_commit_ships_the_full_one(invoke, repo, monkeypatch):
    use_gh(monkeypatch, FakeGh(statuses={repo.a: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--commit", repo.a[:10], "--json")
    assert r.exit_code == 0, r.stdout
    assert released(repo.cloud, "mcp").name == repo.a


@pytest.mark.parametrize("dev_status", [None, "failure", "pending"])
def test_a_commit_dev_did_not_ship_is_refused_before_any_host(
    invoke, repo, monkeypatch, dev_status
):
    statuses = {repo.a: [("vecten/ship/dev", dev_status)]} if dev_status else {}
    gh = use_gh(monkeypatch, FakeGh(statuses=statuses))
    r = invoke("ship", "--env", "prod", "--commit", repo.a, "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert repo.a[:8] in error and "dev" in error
    assert (dev_status or "missing") in error
    assert released(repo.cloud, "mcp") is None
    assert gh.posted_statuses() == []


def test_a_commit_off_the_default_branch_is_refused_before_any_host(invoke, repo, monkeypatch):
    gh = use_gh(monkeypatch, FakeGh(statuses={repo.feature: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--commit", repo.feature, "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert repo.feature[:8] in error and "main" in error
    assert released(repo.cloud, "mcp") is None
    assert gh.posted_statuses() == []


def push_tag_named_main_at(repo: Repo, commit: str) -> None:
    # `git fetch origin main` takes `refs/tags/main` before `refs/heads/main`.
    git(repo.root, "tag", "main", commit)
    git(repo.root, "push", "-q", "origin", "refs/tags/main")
    git(repo.root, "tag", "-d", "main")


def test_a_tag_named_like_the_default_branch_does_not_stand_in_for_it(invoke, repo, monkeypatch):
    push_tag_named_main_at(repo, repo.feature)
    gh = use_gh(monkeypatch, FakeGh(statuses={repo.feature: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--commit", repo.feature, "--json")
    assert r.exit_code == 4, r.stdout
    assert repo.feature[:8] in doc(r)["error"]
    assert released(repo.cloud, "mcp") is None
    assert gh.posted_statuses() == []


def test_the_laptop_checks_fetch_the_default_branch_not_a_tag_of_its_name(repo):
    from vecten.deploy.guards import require_commit_on_default_branch, require_on_default_branch

    push_tag_named_main_at(repo, repo.feature)
    runner = gh_mod.get_git()
    with pytest.raises(Failure) as refused:
        require_commit_on_default_branch(repo.root, runner, "main", repo.feature)
    assert refused.value.code == ExitCode.REFUSED
    git(repo.root, "checkout", "-q", "feature")
    with pytest.raises(Failure) as refused:
        require_on_default_branch(repo.root, "acme/data", FakeGh(), runner)
    assert refused.value.code == ExitCode.REFUSED


def test_the_default_branch_is_read_for_the_repository_the_workflow_runs_in(
    invoke, repo, monkeypatch
):
    # The shipped commit's own `vecten.toml` names a repository its author chose.
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/upstream")
    gh = use_gh(monkeypatch, FakeGh(statuses={repo.b: [DEV_OK]}))
    invoke("ship", "--env", "prod", "--commit", repo.b, "--json")
    viewed = [call[2] for call in gh.calls if call[:2] == ["repo", "view"]]
    assert viewed[:1] == ["acme/upstream"]


def test_the_first_environment_ships_a_given_commit_without_a_status_check(
    invoke, repo, monkeypatch
):
    gh = use_gh(monkeypatch, FakeGh())
    r = invoke("ship", "--env", "dev", "--commit", repo.a, "--json")
    assert r.exit_code == 0, r.stdout
    assert (released(repo.cloud, "mcp") / MARKER).read_text() == "a\n"
    assert gh.named("api", f"repos/acme/data/commits/{repo.a}/status") == []
    assert gh.posted_statuses()[-1] == (repo.a, "vecten/ship/dev", "success")


def test_the_first_environment_still_refuses_a_commit_off_the_default_branch(
    invoke, repo, monkeypatch
):
    use_gh(monkeypatch, FakeGh())
    r = invoke("ship", "--env", "dev", "--commit", repo.feature, "--json")
    assert r.exit_code == 4 and "main" in doc(r)["error"]


def test_a_later_environment_without_commit_checks_the_checked_out_commit(
    invoke, repo, monkeypatch
):
    use_gh(monkeypatch, FakeGh(statuses={repo.a: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert repo.b[:8] in doc(r)["error"]


def test_the_checked_out_commit_ships_from_the_checkout_itself(invoke, repo, monkeypatch):
    gh = use_gh(monkeypatch, FakeGh(statuses={repo.b: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--commit", repo.b, "--json")
    assert r.exit_code == 0, r.stdout
    assert (released(repo.cloud, "mcp") / MARKER).read_text() == "b\n"
    assert gh.posted_statuses()[-1] == (repo.b, "vecten/ship/prod", "success")


# Inside Actions: the order and the facts come from the default branch's head, whatever the
# run checked out


REDEPLOYED = "fedcba9876543210fedcba9876543210fedcba98"


class Adopted:
    """`old` shipped prod before the order existed; `head`, the default branch's head, adopts
    `["dev", "prod"]`; `legacy` is a branch off `old`, with no order, that main never took."""

    root: Path
    cloud: Path
    old: str
    head: str
    legacy: str


@pytest.fixture
def adopted(tmp_path, monkeypatch) -> Adopted:
    root = render_aws(tmp_path / "proj")
    set_facts(root, "prod", bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    (root / MARKER).parent.mkdir(exist_ok=True)
    (root / MARKER).write_text("old\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Promotion")
    git(root, "config", "user.email", "promotion@acme.test")
    old = commit_all(root, "Content before the order")
    ordered(root)
    set_facts(root, "dev", bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    set_facts(root, "prod", deployed_commit=REDEPLOYED)
    (root / MARKER).write_text("head\n")
    head = commit_all(root, "Adopt dev before prod")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "main")
    git(root, "checkout", "-q", "-b", "legacy", old)
    (root / MARKER).write_text("legacy\n")
    legacy = commit_all(root, "Unreviewed, without the order")
    git(root, "checkout", "-q", "main")

    cloud = tmp_path / "cloud"
    monkeypatch.syspath_prepend(str(MOCK_DIR))
    monkeypatch.setenv("VECTEN_CONTROL_PLANE", "vecten_mock_cloud:MockCloud")
    monkeypatch.setenv("VECTEN_MOCK_CLOUD_DIR", str(cloud))
    monkeypatch.setattr("vecten_shared.provider._REGISTRY", dict(_REGISTRY))
    from vecten_mock_cloud import MockCloud

    for env in ("dev", "prod"):
        MockCloud(root, env).store_outputs(MockCloud.OUTPUTS)
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/data")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("VECTEN_GIT_RUNNER", raising=False)
    found = Adopted()
    found.root, found.cloud, found.old, found.head, found.legacy = root, cloud, old, head, legacy
    return found


def no_terraform(monkeypatch) -> None:
    def provision(plane, env, terraform):
        raise AssertionError("terraform ran")

    monkeypatch.setattr(run_mod, "provision_environment", provision)
    monkeypatch.setattr(run_mod, "require_terraform", lambda terraform: None)


def test_a_rerun_ship_of_a_commit_from_before_the_order_needs_dev(invoke, adopted, monkeypatch):
    # A re-run of a push's ship run checks out the pushed commit and passes it as --commit;
    # that commit declares no order, so it reads prod as the first environment.
    git(adopted.root, "checkout", "-q", "--detach", adopted.old)
    gh = use_gh(monkeypatch, FakeGh(history=[adopted.head, adopted.old]))
    r = invoke("ship", "--env", "prod", "--commit", adopted.old, "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert adopted.old[:8] in error and "vecten/ship/dev" in error
    assert released(adopted.cloud, "mcp") is None
    assert gh.posted_statuses() == []


def test_a_rerun_deploy_of_a_head_from_before_the_order_needs_dev(invoke, adopted, monkeypatch):
    # A re-run of a dispatched apply run checks out the head it first ran on, without --commit.
    git(adopted.root, "checkout", "-q", "--detach", adopted.old)
    no_terraform(monkeypatch)
    gh = use_gh(monkeypatch, FakeGh(history=[adopted.head, adopted.old]))
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert adopted.old[:8] in doc(r)["error"] and "dev" in doc(r)["error"]
    assert gh.posted_statuses() == []


def test_a_dispatch_on_a_branch_without_the_order_is_refused(invoke, adopted, monkeypatch):
    # A workflow_dispatch on another ref checks that ref out and passes no --commit.
    git(adopted.root, "checkout", "-q", "legacy")
    gh = use_gh(monkeypatch, FakeGh(statuses={adopted.legacy: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    error = doc(r)["error"]
    assert adopted.legacy[:8] in error and "origin/main" in error
    assert released(adopted.cloud, "mcp") is None
    assert gh.posted_statuses() == []


def test_a_dispatch_on_a_branch_is_refused_for_the_first_environment(invoke, repo, monkeypatch):
    # A workflow_dispatch on a feature branch, without --commit, ships that branch's head.
    git(repo.root, "checkout", "-q", "feature")
    gh = use_gh(monkeypatch, FakeGh())
    r = invoke("ship", "--env", "dev", "--json")
    assert r.exit_code == 4, r.stdout
    assert repo.feature[:8] in doc(r)["error"] and "origin/main" in doc(r)["error"]
    assert released(repo.cloud, "mcp") is None
    assert gh.posted_statuses() == []


def test_a_rerun_takes_the_facts_from_the_heads_vecten_toml(invoke, adopted, monkeypatch):
    # The old commit records prod's deploy; the head, where the CLI records facts, says prod
    # was destroyed since.
    set_facts(adopted.root, "prod", deployed_commit=None)
    commit_all(adopted.root, "prod destroyed")
    git(adopted.root, "push", "-q", "origin", "main")
    git(adopted.root, "checkout", "-q", "--detach", adopted.old)
    gh = use_gh(monkeypatch, FakeGh(statuses={adopted.old: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--commit", adopted.old, "--json")
    assert r.exit_code == 4, r.stdout
    assert "vecten deploy --env prod" in doc(r)["error"]
    assert released(adopted.cloud, "mcp") is None
    assert gh.posted_statuses() == []


def test_a_commit_dev_shipped_from_before_the_order_ships_to_prod(invoke, adopted, monkeypatch):
    git(adopted.root, "checkout", "-q", "--detach", adopted.old)
    gh = use_gh(monkeypatch, FakeGh(statuses={adopted.old: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--commit", adopted.old, "--json")
    assert r.exit_code == 0, r.stdout
    release = released(adopted.cloud, "mcp")
    assert (release / MARKER).read_text() == "old\n"
    # The old commit records prod's first deploy; the head records its latest.
    assert load_config(release).environments["prod"].deployed_commit == REDEPLOYED
    assert gh.posted_statuses()[-1] == (adopted.old, "vecten/ship/prod", "success")
    assert git(adopted.root, "worktree", "list").count("\n") == 0


# Inside Actions: a deploy's git sources are fetched and checked before Terraform


def test_a_deploy_whose_git_source_fails_to_fetch_runs_no_terraform(invoke, repo, monkeypatch):
    fetched = []

    def write_sources(root, config, into, **kwargs):
        fetched.append(into)
        raise Failure(ExitCode.TOOL_FAILED, "git fetch of acme-resolution failed (exit 128)")

    monkeypatch.setattr(git_extensions, "write_sources", write_sources)
    no_terraform(monkeypatch)
    gh = use_gh(monkeypatch, FakeGh(statuses={repo.a: [DEV_OK]}))
    r = invoke("deploy", "--env", "prod", "--commit", repo.a, "--json")
    assert r.exit_code == 5, r.stdout
    assert "acme-resolution" in doc(r)["error"]
    assert len(fetched) == 1
    assert released(repo.cloud, "mcp") is None
    assert gh.posted_statuses() == []


def test_a_deploy_ships_the_git_sources_it_fetched_before_terraform(invoke, repo, monkeypatch):
    from vecten_mock_cloud import MockCloud

    events = []
    fetch = git_extensions.write_sources

    def write_sources(*args, **kwargs):
        events.append("sources")
        return fetch(*args, **kwargs)

    def provision(plane, env, terraform):
        events.append("terraform")
        return {"roots": ["aws"], "outputs": MockCloud.OUTPUTS}

    monkeypatch.setattr(git_extensions, "write_sources", write_sources)
    monkeypatch.setattr(run_mod, "provision_environment", provision)
    monkeypatch.setattr(run_mod, "require_terraform", lambda terraform: None)
    use_gh(monkeypatch, FakeGh(statuses={repo.a: [DEV_OK]}))
    r = invoke("deploy", "--env", "prod", "--commit", repo.a, "--json")
    assert r.exit_code == 0, r.stdout
    assert events == ["sources", "terraform"]
    assert (released(repo.cloud, "mcp") / MARKER).read_text() == "a\n"


def test_a_commit_that_is_no_commit_id_is_usage(invoke, repo, monkeypatch):
    use_gh(monkeypatch, FakeGh())
    r = invoke("ship", "--env", "prod", "--commit", "HEAD~1", "--json")
    assert r.exit_code == 2, r.stdout
    assert "--commit" in doc(r)["error"]


# Inside Actions: `deploy --env <env> --commit <sha>`


def test_a_promoted_deploy_applies_the_commits_roots_and_ships_it(invoke, repo, monkeypatch):
    from vecten_mock_cloud import MockCloud

    gh = use_gh(monkeypatch, FakeGh(statuses={repo.a: [DEV_OK]}))
    applied = []

    def provision(plane, env, terraform):
        applied.append((env, (plane.project_root / "deploy" / "envs" / "prod.tfvars").read_text()))
        return {"roots": ["aws"], "outputs": MockCloud.OUTPUTS}

    monkeypatch.setattr(run_mod, "provision_environment", provision)
    monkeypatch.setattr(run_mod, "require_terraform", lambda terraform: None)
    r = invoke("deploy", "--env", "prod", "--commit", repo.a, "--json")
    assert r.exit_code == 0, r.stdout
    [(env, tfvars)] = applied
    assert env == "prod" and "# written after a" not in tfvars
    assert doc(r)["commit"] == repo.a
    assert (released(repo.cloud, "mcp") / MARKER).read_text() == "a\n"
    assert gh.posted_statuses()[-1] == (repo.a, "vecten/ship/prod", "success")


def test_a_promoted_deploy_of_a_commit_dev_did_not_ship_runs_no_terraform(
    invoke, repo, monkeypatch
):
    use_gh(monkeypatch, FakeGh())

    def provision(plane, env, terraform):
        raise AssertionError("terraform ran")

    monkeypatch.setattr(run_mod, "provision_environment", provision)
    monkeypatch.setattr(run_mod, "require_terraform", lambda terraform: None)
    r = invoke("deploy", "--env", "prod", "--commit", repo.a, "--json")
    assert r.exit_code == 4, r.stdout
    assert "dev" in doc(r)["error"]


# From the laptop


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    root = ordered(render_aws(tmp_path / "proj"))
    for env in ("dev", "prod"):
        set_facts(root, env, bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    return root


SHIPPED = "a3f9c2e0a3f9c2e0a3f9c2e0a3f9c2e0a3f9c2e0"
HEAD = "b71d004b71d004b71d004b71d004b71d004b71d0"


def test_ship_of_a_later_environment_from_the_laptop_names_promote(invoke, laptop, monkeypatch):
    gh = use_gh(monkeypatch, FakeGh())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "vecten promote --env prod" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_ship_of_the_first_environment_from_the_laptop_dispatches_as_before(
    invoke, laptop, monkeypatch
):
    gh = use_gh(monkeypatch, FakeGh())
    r = invoke("ship", "--env", "dev", "--json")
    assert r.exit_code == 0, r.stdout
    [dispatch] = gh.named("workflow", "run")
    assert dispatch == [
        "workflow",
        "run",
        "deploy-platform.yml",
        "--ref",
        "main",
        "-f",
        "environment=dev",
        "--repo",
        "acme/data",
    ]


def test_ship_with_commit_from_the_laptop_is_usage(invoke, laptop, monkeypatch):
    gh = use_gh(monkeypatch, FakeGh())
    r = invoke("ship", "--env", "dev", "--commit", SHIPPED, "--json")
    assert r.exit_code == 2, r.stdout
    assert "GitHub Actions" in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_the_gate_without_env_reports_the_first_environment(invoke, laptop, tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/data")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    r = invoke("ship", "--check", "--json")
    assert r.exit_code == 0, r.stdout
    assert doc(r) == {"env": "dev", "bootstrapped": True, "deployed": True, "ok": True}
    assert out.read_text() == "env=dev\nbootstrapped=true\ndeployed=true\n"


def test_the_gate_without_env_and_without_an_order_reports_prod(invoke, tmp_path, monkeypatch):
    root = render_aws(tmp_path / "plain")
    set_facts(root, "prod", bootstrapped=True, state_bucket="mock-state")
    monkeypatch.chdir(root)
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    r = invoke("ship", "--check")
    assert r.exit_code == 0, r.stdout
    assert r.stdout.strip() == "env=prod bootstrapped=true deployed=false"
    assert out.read_text() == "env=prod\nbootstrapped=true\ndeployed=false\n"


def test_ship_without_env_and_without_check_still_needs_env(invoke, laptop):
    r = invoke("ship", "--json")
    assert r.exit_code == 2 and "--env" in doc(r)["error"]


def _dispatched_commit(gh: FakeGh) -> list[str]:
    [dispatch] = gh.named("workflow", "run")
    return [a for a in dispatch if a.startswith("commit=")]


def test_deploy_of_a_later_environment_ships_what_dev_shipped_last(invoke, laptop, monkeypatch):
    gh = use_gh(
        monkeypatch,
        FakeGh(history=[HEAD, SHIPPED], statuses={SHIPPED: [DEV_OK]}),
    )
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    [dispatch] = gh.named("workflow", "run")
    assert dispatch[:9] == [
        "workflow",
        "run",
        "terraform-apply.yml",
        "--ref",
        "main",
        "-f",
        "environment=prod",
        "-f",
        f"commit={SHIPPED}",
    ]
    # The run's own head is the default branch's; what it applied and shipped is SHIPPED.
    assert load_config(laptop).environments["prod"].deployed_commit == SHIPPED
    data = doc(r)
    assert data["deployed_commit"] == SHIPPED and data["commit"] == SHIPPED
    assert data["head_sha"] == COMMIT


def test_deploy_of_a_later_environment_with_commit_ships_that_commit(invoke, laptop, monkeypatch):
    gh = use_gh(monkeypatch, FakeGh(statuses={SHIPPED: [DEV_OK]}))
    git = FakeGit(answers={("rev-parse", "--verify"): (0, SHIPPED + "\n")})
    monkeypatch.setattr(gh_mod, "get_git", lambda: git)
    r = invoke("deploy", "--env", "prod", "--commit", SHIPPED[:7], "--json")
    assert r.exit_code == 0, r.stdout
    assert _dispatched_commit(gh) == [f"commit={SHIPPED}"]
    assert load_config(laptop).environments["prod"].deployed_commit == SHIPPED


def test_deploy_of_a_later_environment_refuses_a_commit_dev_did_not_ship(
    invoke, laptop, monkeypatch
):
    gh = use_gh(monkeypatch, FakeGh())
    git = FakeGit(answers={("rev-parse", "--verify"): (0, SHIPPED + "\n")})
    monkeypatch.setattr(gh_mod, "get_git", lambda: git)
    r = invoke("deploy", "--env", "prod", "--commit", SHIPPED, "--json")
    assert r.exit_code == 4, r.stdout
    assert "dev" in doc(r)["error"] and SHIPPED[:8] in doc(r)["error"]
    assert gh.named("workflow", "run") == []


def test_deploy_of_a_later_environment_before_dev_shipped_names_dev(invoke, laptop, monkeypatch):
    gh = use_gh(monkeypatch, FakeGh(history=[HEAD]))
    r = invoke("deploy", "--env", "prod", "--json")
    assert r.exit_code == 3, r.stdout
    assert "dev" in doc(r)["error"]
    assert gh.named("workflow", "run") == []
    assert load_config(laptop).environments["prod"].deployed_commit == COMMIT


def test_deploy_of_the_first_environment_dispatches_as_before(invoke, laptop, monkeypatch):
    gh = use_gh(monkeypatch, FakeGh())
    r = invoke("deploy", "--env", "dev", "--json")
    assert r.exit_code == 0, r.stdout
    [dispatch] = gh.named("workflow", "run")
    assert dispatch == [
        "workflow",
        "run",
        "terraform-apply.yml",
        "--ref",
        "main",
        "-f",
        "environment=dev",
        "--repo",
        "acme/data",
    ]
    assert "commit" not in doc(r)


def test_deploy_with_commit_of_the_first_environment_is_usage_from_the_laptop(
    invoke, laptop, monkeypatch
):
    gh = use_gh(monkeypatch, FakeGh())
    r = invoke("deploy", "--env", "dev", "--commit", SHIPPED, "--json")
    assert r.exit_code == 2, r.stdout
    assert gh.named("workflow", "run") == []


# A project without an order


def test_without_an_order_ship_and_deploy_dispatch_exactly_as_before(invoke, tmp_path, monkeypatch):
    root = render_aws(tmp_path / "plain")
    set_facts(root, "prod", bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(github_env, "sleep", lambda seconds: None)
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    gh = use_gh(monkeypatch, FakeGh())
    shipped = invoke("ship", "--env", "prod", "--json")
    deployed = invoke("deploy", "--env", "prod", "--json")
    assert shipped.exit_code == 0 and deployed.exit_code == 0
    expected = ["-f", "environment=prod", "--repo", "acme/data"]
    assert [call[5:] for call in gh.named("workflow", "run")] == [expected, expected]
    assert gh.named("api") == []
    assert set(doc(shipped)) == {
        "ok",
        "env",
        "dispatched",
        "run_id",
        "head_sha",
        "conclusion",
        "url",
        "branch",
        "matches_local_head",
    }
    assert "commit" not in doc(deployed)
    assert load_config(root).environments["prod"].deployed_commit == COMMIT


# A newer head than the promoted commit's CLI knows


def test_an_unknown_fact_on_the_head_is_named_and_the_promotion_ships(invoke, repo, monkeypatch):
    toml = repo.root / "vecten.toml"
    toml.write_text(
        toml.read_text().replace(
            "[environments.prod]\n", "[environments.prod]\nfact_from_a_newer_vecten = true\n", 1
        )
    )
    assert "fact_from_a_newer_vecten" in toml.read_text()
    head = commit_all(repo.root, "A fact only a newer vecten records")
    git(repo.root, "push", "-q", "origin", "main")
    gh = use_gh(monkeypatch, FakeGh(history=[head, repo.b, repo.a], statuses={repo.a: [DEV_OK]}))
    r = invoke("ship", "--env", "prod", "--commit", repo.a, "--json")
    assert r.exit_code == 0, r.stdout
    [warning] = doc(r)["warnings"]
    assert "fact_from_a_newer_vecten" in warning and "environments.prod" in warning
    shipped = load_config(released(repo.cloud, "mcp"))
    assert shipped.environments["prod"].deployed_commit == COMMIT
    assert gh.posted_statuses()[-1] == (repo.a, "vecten/ship/prod", "success")


def test_an_environment_another_promotes_from_fails_closed_without_its_status(
    invoke, repo, monkeypatch
):
    # prod promotes from dev, so a dev ship that cannot be recorded could never be promoted.
    use_gh(monkeypatch, FakeGh(failing=("api",)))
    r = invoke("ship", "--env", "dev", "--commit", repo.a, "--json")
    assert r.exit_code == 5, r.stdout
    error = doc(r)["error"]
    assert "status" in error and repo.a in error
    assert released(repo.cloud, "mcp") is None


def test_a_later_environment_from_the_laptop_names_promote_before_deployed(
    invoke, laptop, monkeypatch
):
    set_facts(laptop, "prod", deployed_commit=None)
    gh = use_gh(monkeypatch, FakeGh())
    r = invoke("ship", "--env", "prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "vecten promote --env prod" in doc(r)["error"]
    assert gh.calls == []


def test_a_promoted_deploy_also_reads_the_heads_facts_leniently(invoke, repo, monkeypatch):
    from vecten_mock_cloud import MockCloud

    toml = repo.root / "vecten.toml"
    toml.write_text(
        toml.read_text().replace(
            "[environments.prod]\n", "[environments.prod]\nfact_from_a_newer_vecten = 1\n", 1
        )
    )
    commit_all(repo.root, "A fact only a newer vecten records")
    git(repo.root, "push", "-q", "origin", "main")
    use_gh(monkeypatch, FakeGh(statuses={repo.a: [DEV_OK]}))
    monkeypatch.setattr(
        run_mod,
        "provision_environment",
        lambda plane, env, terraform: {"roots": ["aws"], "outputs": MockCloud.OUTPUTS},
    )
    monkeypatch.setattr(run_mod, "require_terraform", lambda terraform: None)
    r = invoke("deploy", "--env", "prod", "--commit", repo.a, "--json")
    assert r.exit_code == 0, r.stdout
    assert any("fact_from_a_newer_vecten" in w for w in doc(r)["warnings"])
    assert (released(repo.cloud, "mcp") / MARKER).read_text() == "a\n"
