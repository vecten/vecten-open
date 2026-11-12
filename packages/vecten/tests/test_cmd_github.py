import json
import subprocess

import pytest

from vecten.github import gh as gh_mod
from vecten_shared.config import EnvironmentFacts, load_config, save_config

from .test_deploy_promotion import ordered

LOCAL = ("--name", "acme", "--users", "8", "--source", "hubspot")
DEPLOY = (
    "--cloud",
    "aws",
    "--domain",
    "data.acme.com",
    "--dns-zone",
    "acme.com",
    "--github-repository",
    "acme/data",
)


class FakeGh:
    def __init__(self, logged_in=True, present=True):
        self.logged_in, self.present = logged_in, present
        self.calls = []

    def which(self):
        return "/usr/bin/gh" if self.present else None

    def run(self, args, cwd):
        self.calls.append(args)
        if args[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                args, 0 if self.logged_in else 1, "", "" if self.logged_in else "not logged in"
            )
        if args[:2] == ["repo", "create"]:
            subprocess.run(
                ["git", "remote", "add", "origin", str(cwd / ".." / "remote.git")], cwd=cwd
            )
            subprocess.run(["git", "push", "-u", "origin", "HEAD"], cwd=cwd, capture_output=True)
            return subprocess.CompletedProcess(args, 0, "https://github.com/acme/data", "")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def project(invoke, workdir, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(workdir / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    invoke("init", "p", *LOCAL, *DEPLOY, "--json")
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(workdir / "remote.git")],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(workdir / "p")
    return workdir / "p"


def remote_git(project, *args):
    return subprocess.run(
        ["git", "--git-dir", str(project.parent / "remote.git"), *args],
        capture_output=True,
        text=True,
    )


def test_github_init_creates_and_pushes(invoke, project, monkeypatch):
    (project / ".env").write_text("SECRET=1\n")
    fake = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: fake)
    r = invoke("github", "init", "--json")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["repository"] == "acme/data" and doc["pushed"] is True
    assert doc["committed"] is True
    assert [
        "repo",
        "create",
        "acme/data",
        "--private",
        "--source",
        ".",
        "--remote",
        "origin",
        "--push",
    ] in fake.calls
    assert "vecten init" in remote_git(project, "log", "--oneline").stdout
    tree = remote_git(project, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
    assert "vecten.toml" in tree
    assert ".env" not in tree and not any(p.startswith(".vecten/") for p in tree)


def test_github_push_commits_and_pushes_changes(invoke, project, monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: fake)
    invoke("github", "init", "--json")
    (project / "connectors" / "hubspot.py").write_text("x = 1\n")
    r = invoke("github", "push", "--json")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["pushed"] is True and doc["committed"] is True
    assert remote_git(project, "log", "--oneline").stdout.count("\n") == 2


def test_github_push_with_no_changes_exits_zero_without_a_commit(invoke, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh())
    invoke("github", "init", "--json")
    before = remote_git(project, "log", "--oneline").stdout
    r = invoke("github", "push", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["committed"] is False
    assert remote_git(project, "log", "--oneline").stdout == before


SHIP_URL = "https://github.com/acme/data/actions/runs/77"


class ShipGh(FakeGh):
    """FakeGh whose ship workflow run appears after `appear_after` listings and ends with
    `conclusion`; `appear_after=None` means it never appears."""

    def __init__(self, conclusion="success", appear_after=0):
        super().__init__()
        self.conclusion, self.appear_after, self.listed = conclusion, appear_after, 0

    def run(self, args, cwd):
        if args[:2] == ["run", "list"]:
            self.calls.append(args)
            self.listed += 1
            ready = self.appear_after is not None and self.listed > self.appear_after
            runs = [{"databaseId": 77, "url": SHIP_URL}] if ready else []
            return subprocess.CompletedProcess(args, 0, json.dumps(runs), "")
        if args[:2] == ["run", "watch"]:
            self.calls.append(args)
            code = 0 if self.conclusion == "success" else 1
            return subprocess.CompletedProcess(args, code, "", "")
        if args[:2] == ["run", "view"]:
            self.calls.append(args)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True
            ).stdout.strip()
            body = {"conclusion": self.conclusion, "url": SHIP_URL, "headSha": head}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        return super().run(args, cwd)

    def run_calls(self):
        return [call for call in self.calls if call[0] == "run"]


def deploy_and_change(invoke, project, fake, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: fake)
    monkeypatch.setattr("vecten.deploy.github_env.sleep", lambda seconds: None)
    invoke("github", "init", "--json")
    config = load_config(project)
    config.environments.setdefault("prod", EnvironmentFacts()).deployed_commit = "abc123"
    save_config(project, config)


def test_push_of_a_deployed_project_waits_for_its_ship(invoke, project, monkeypatch):
    fake = ShipGh()
    deploy_and_change(invoke, project, fake, monkeypatch)
    r = invoke("github", "push", "--json")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["ship"] == {"run_url": SHIP_URL, "conclusion": "success"}
    listing, watch = fake.run_calls()[0], fake.run_calls()[-2]
    assert listing[listing.index("--workflow") + 1] == "ship.yml"
    assert listing[listing.index("--commit") + 1] == doc["commit"]
    assert watch[:4] == ["run", "watch", "77", "--exit-status"]


def test_push_of_a_project_never_deployed_does_not_wait(invoke, project, monkeypatch):
    fake = ShipGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: fake)
    invoke("github", "init", "--json")
    (project / "connectors" / "hubspot.py").write_text("x = 1\n")
    r = invoke("github", "push", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["ship"] is None
    assert fake.run_calls() == []


def test_a_failed_ship_names_its_run_and_keeps_the_push(invoke, project, monkeypatch):
    fake = ShipGh(conclusion="failure")
    deploy_and_change(invoke, project, fake, monkeypatch)
    r = invoke("github", "push", "--json")
    assert r.exit_code == 5
    assert SHIP_URL in json.loads(r.stdout)["error"]
    assert "abc123" in remote_git(project, "show", "HEAD:vecten.toml").stdout


def test_no_wait_pushes_without_following_the_ship(invoke, project, monkeypatch):
    fake = ShipGh()
    deploy_and_change(invoke, project, fake, monkeypatch)
    r = invoke("github", "push", "--no-wait", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["ship"] is None
    assert fake.run_calls() == []


def test_a_ship_run_that_never_appears_fails_naming_the_workflow(invoke, project, monkeypatch):
    fake = ShipGh(appear_after=None)
    deploy_and_change(invoke, project, fake, monkeypatch)
    r = invoke("github", "push", "--json")
    assert r.exit_code == 5
    assert "ship.yml" in json.loads(r.stdout)["error"]
    assert all(call[:2] == ["run", "list"] for call in fake.run_calls())


def test_a_push_that_moves_nothing_starts_no_ship_to_wait_for(invoke, project, monkeypatch):
    fake = ShipGh()
    deploy_and_change(invoke, project, fake, monkeypatch)
    assert invoke("github", "push", "--json").exit_code == 0
    fake.calls.clear()
    r = invoke("github", "push", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["ship"] is None
    assert fake.run_calls() == []


def test_github_init_twice_refuses_the_existing_origin(invoke, project, monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(gh_mod, "get_gh", lambda: fake)
    assert invoke("github", "init", "--json").exit_code == 0
    r = invoke("github", "init", "--json")
    assert r.exit_code == 4 and "github push" in json.loads(r.stdout)["error"]
    assert sum(1 for call in fake.calls if call[:2] == ["repo", "create"]) == 1


def test_github_push_before_init_is_refused(invoke, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh())
    r = invoke("github", "push", "--json")
    assert r.exit_code == 4 and "github init" in json.loads(r.stdout)["error"]


def test_github_push_inside_an_enclosing_repository_is_refused(invoke, project, monkeypatch):
    outer = project.parent
    subprocess.run(["git", "init", "-b", "main"], cwd=outer, check=True, capture_output=True)
    (outer / "README").write_text("outer\n")
    subprocess.run(["git", "add", "README"], cwd=outer, check=True)
    subprocess.run(["git", "commit", "-m", "outer"], cwd=outer, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=outer, capture_output=True, text=True)
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh())
    r = invoke("github", "push", "--json")
    assert r.exit_code == 4 and "github init" in json.loads(r.stdout)["error"]
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=outer, capture_output=True, text=True)
    assert after.stdout == head.stdout
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=outer, capture_output=True, text=True
    )
    assert staged.stdout == ""


def test_not_logged_in_names_gh_auth_login(invoke, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(logged_in=False))
    r = invoke("github", "init", "--json")
    error = json.loads(r.stdout)["error"]
    assert r.exit_code == 3 and "gh auth login" in error and "not logged in" in error


def test_gh_missing(invoke, project, monkeypatch):
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh(present=False))
    r = invoke("github", "push", "--json")
    error = json.loads(r.stdout)["error"]
    assert r.exit_code == 3 and "gh auth login" in error and "not installed" in error


def test_init_without_repository_answer(invoke, workdir, monkeypatch):
    invoke("init", "q", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "q")
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh())
    r = invoke("github", "init", "--json")
    assert r.exit_code == 4 and "init --deploy" in json.loads(r.stdout)["error"]


def deploy_in_order(invoke, project, fake, monkeypatch, deployed):
    """Declare the order dev, prod and record a deploy of each environment in `deployed`."""
    monkeypatch.setattr(gh_mod, "get_gh", lambda: fake)
    monkeypatch.setattr("vecten.deploy.github_env.sleep", lambda seconds: None)
    invoke("github", "init", "--json")
    ordered(project)
    config = load_config(project)
    for env in deployed:
        config.environments[env].deployed_commit = "abc123"
    save_config(project, config)


def test_a_push_with_an_order_ships_dev_and_names_the_promotion_to_prod(
    invoke, project, monkeypatch
):
    fake = ShipGh()
    deploy_in_order(invoke, project, fake, monkeypatch, deployed=("dev",))
    r = invoke("github", "push", "--json")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["ship"] == {"run_url": SHIP_URL, "conclusion": "success"}
    assert doc["promote"] == "vecten promote --env prod"
    invoke("github", "push", "--json")
    (project / "connectors" / "hubspot.py").write_text("x = 2\n")
    text = invoke("github", "push")
    assert text.exit_code == 0, text.stdout
    assert "`vecten promote --env prod`" in text.stdout


def test_a_push_waits_only_once_the_first_environment_is_deployed(invoke, project, monkeypatch):
    fake = ShipGh()
    deploy_in_order(invoke, project, fake, monkeypatch, deployed=("prod",))
    r = invoke("github", "push", "--json")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["ship"] is None and doc["promote"] is None
    assert fake.run_calls() == []


def test_a_push_without_an_order_names_no_promotion(invoke, project, monkeypatch):
    fake = ShipGh()
    deploy_and_change(invoke, project, fake, monkeypatch)
    r = invoke("github", "push", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["promote"] is None
    (project / "connectors" / "hubspot.py").write_text("x = 2\n")
    text = invoke("github", "push")
    assert text.exit_code == 0, text.stdout
    assert text.stdout.strip().endswith(f"shipped: {SHIP_URL}")
