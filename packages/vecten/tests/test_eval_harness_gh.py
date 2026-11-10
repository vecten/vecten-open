"""The evaluation harness's `gh` stand-in: every call `vecten` makes, and the pull requests the
`operate` and `semantic` skills open, answered by an executable first on the PATH."""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import gh_standin  # noqa: E402

from .deploy_fixtures import render_aws  # noqa: E402

REPO = "acme/data"
SRC = Path(__file__).parents[1] / "src" / "vecten"
SECRET_VALUE = "never-in-the-log-7c4e"


@pytest.fixture
def gh_env(tmp_path, monkeypatch):
    """The environment an agent's shell gets: the stand-in first on the PATH, git isolated."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = Ada\n\temail = ada@acme.test\n")
    env = {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    env.pop("GITHUB_ACTIONS", None)
    env.update(gh_standin.install(tmp_path / "github", python=sys.executable, environ=env))
    return env


def gh(env, *args, cwd=None, input=None, check=True):
    result = subprocess.run(
        ["gh", *args], cwd=cwd, env=env, input=input, capture_output=True, text=True
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def git(env, cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def calls(env):
    path = Path(env["EVAL_HARNESS_GH_DIR"]) / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def checkout(tmp_path, gh_env):
    root = tmp_path / "proj"
    root.mkdir()
    git(gh_env, root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("acme\n")
    git(gh_env, root, "add", "-A")
    git(gh_env, root, "commit", "-q", "-m", "initial")
    gh(gh_env, "repo", "create", REPO, "--private", "--source", ".", "--remote", "origin", cwd=root)
    gh(gh_env, "repo", "create", "acme/other", "--private")
    git(gh_env, root, "push", "-q", "-u", "origin", "HEAD")
    return root


def test_the_standin_answers_first_on_the_path_and_logs_each_call(gh_env, tmp_path):
    assert gh(gh_env, "auth", "status").returncode == 0
    assert calls(gh_env) == [
        {**calls(gh_env)[0], "argv": ["auth", "status"], "exit": 0, "answered": True}
    ]


def test_a_call_the_standin_does_not_answer_fails_and_is_logged(gh_env):
    result = gh(gh_env, "codespace", "list", check=False)
    assert result.returncode == 1 and "does not answer" in result.stderr
    assert calls(gh_env)[-1]["answered"] is False


def test_repo_create_pushes_to_a_local_remote_spelled_as_on_github(tmp_path, gh_env):
    root = tmp_path / "p"
    root.mkdir()
    git(gh_env, root, "init", "-q", "-b", "main")
    (root / "a").write_text("a\n")
    git(gh_env, root, "add", "-A")
    git(gh_env, root, "commit", "-q", "-m", "a")
    args = ["repo", "create", REPO, "--private", "--source", ".", "--remote", "origin", "--push"]
    assert gh(gh_env, *args, cwd=root).stdout.strip() == f"https://github.com/{REPO}"
    configured = git(gh_env, root, "config", "--get", "remote.origin.url")
    assert configured == f"https://github.com/{REPO}.git"
    head = git(gh_env, root, "rev-parse", "HEAD")
    assert head in git(gh_env, root, "ls-remote", "origin", "main")
    again = gh(gh_env, *args, cwd=root, check=False)
    assert again.returncode == 1 and "already exists" in again.stderr


def test_a_global_rewrite_of_github_to_ssh_does_not_take_pushes_away(tmp_path, gh_env):
    """A person's git commonly sends https://github.com/ over SSH; the stand-in's rewrite has
    to win, or an evaluation would push to the real GitHub."""
    home = Path(gh_env["HOME"])
    with (home / ".gitconfig").open("a") as config:
        config.write('[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n')
    env = {**gh_env, "GIT_SSH_COMMAND": "false"}
    root = tmp_path / "p"
    root.mkdir()
    git(env, root, "init", "-q", "-b", "main")
    (root / "a").write_text("a\n")
    git(env, root, "add", "-A")
    git(env, root, "commit", "-q", "-m", "a")
    gh(
        env,
        "repo",
        "create",
        REPO,
        "--private",
        "--source",
        ".",
        "--remote",
        "origin",
        "--push",
        cwd=root,
    )
    head = git(env, root, "rev-parse", "HEAD")
    assert head in git(env, root, "ls-remote", "origin", "main")


def test_repo_view_and_the_repository_api(checkout, gh_env):
    view = gh(gh_env, "repo", "view", REPO, "--json", "nameWithOwner,defaultBranchRef")
    assert json.loads(view.stdout) == {
        "nameWithOwner": REPO,
        "defaultBranchRef": {"name": "main"},
    }
    here = gh(gh_env, "repo", "view", "--json", "nameWithOwner", cwd=checkout)
    assert json.loads(here.stdout) == {"nameWithOwner": REPO}
    placeholders = gh(gh_env, "api", "repos/{owner}/{repo}", cwd=checkout)
    assert json.loads(placeholders.stdout)["full_name"] == REPO
    body = json.loads(gh(gh_env, "api", f"repos/{REPO}").stdout)
    assert body["full_name"] == REPO and type(body["id"]) is int
    assert type(body["owner"]["id"]) is int and body["owner"]["login"] == "acme"
    missing = gh(gh_env, "repo", "view", "acme/nope", "--json", "nameWithOwner", check=False)
    assert missing.returncode == 1 and "Could not resolve" in missing.stderr


def test_environments_answer_404_until_created_then_hold_their_branch_policy(checkout, gh_env):
    path = f"repos/{REPO}/environments/prod"
    missing = gh(gh_env, "api", path, check=False)
    assert missing.returncode == 1 and "HTTP 404" in missing.stderr
    gh(
        gh_env,
        "api",
        "-X",
        "PUT",
        path,
        "-F",
        "deployment_branch_policy[protected_branches]=false",
        "-F",
        "deployment_branch_policy[custom_branch_policies]=true",
    )
    gh(gh_env, "api", "-X", "POST", f"{path}/deployment-branch-policies", "-f", "name=main")
    body = json.loads(gh(gh_env, "api", path).stdout)
    assert body["deployment_branch_policy"] == {
        "protected_branches": False,
        "custom_branch_policies": True,
    }
    policies = json.loads(gh(gh_env, "api", f"{path}/deployment-branch-policies").stdout)
    assert [p["name"] for p in policies["branch_policies"]] == ["main"]


def test_secret_values_arrive_on_stdin_and_are_never_written(checkout, gh_env):
    gh(gh_env, "secret", "set", "TOKEN", "--env", "prod", "--repo", REPO, input=SECRET_VALUE)
    gh(gh_env, "variable", "set", "REGION", "--env", "prod", "--body", "eu", "--repo", REPO)
    listed = gh(gh_env, "secret", "list", "--env", "prod", "--repo", REPO).stdout
    assert "TOKEN" in listed
    for path in Path(gh_env["EVAL_HARNESS_GH_DIR"]).rglob("*"):
        if path.is_file():
            assert SECRET_VALUE.encode() not in path.read_bytes(), path
    logged = calls(gh_env)[-3]
    assert logged["argv"][:2] == ["secret", "set"] and logged["stdin"] is True


def test_a_secret_passed_as_an_argument_is_redacted_in_the_log(checkout, gh_env):
    gh(gh_env, "secret", "set", "TOKEN", "--body", SECRET_VALUE, "--repo", REPO)
    assert SECRET_VALUE not in (Path(gh_env["EVAL_HARNESS_GH_DIR"]) / "calls.jsonl").read_text()
    assert calls(gh_env)[-1]["value_in_argv"] is True


def open_pull(env, root, branch, body, title="Fix"):
    git(env, root, "checkout", "-q", "-b", branch)
    (root / f"{branch.replace('/', '-')}.txt").write_text(body)
    git(env, root, "add", "-A")
    git(env, root, "commit", "-q", "-m", title)
    unpushed = gh(env, "pr", "create", "--title", title, "--body", body, cwd=root, check=False)
    assert unpushed.returncode == 1 and "push" in unpushed.stderr
    git(env, root, "push", "-q", "-u", "origin", branch)
    body_file = root.parent / "body.md"
    body_file.write_text(body)
    created = gh(env, "pr", "create", "--title", title, "--body-file", str(body_file), cwd=root)
    git(env, root, "checkout", "-q", "main")
    return int(created.stdout.strip().rsplit("/", 1)[1])


def test_a_pull_request_opens_lists_reviews_merges_and_reads_back(checkout, gh_env):
    number = open_pull(gh_env, checkout, "semantic/gap-refunds", "Fixes gap-refunds.\n")
    listed = gh(gh_env, "pr", "list", "--json", "number,headRefName,state", cwd=checkout)
    assert json.loads(listed.stdout) == [
        {"number": number, "headRefName": "semantic/gap-refunds", "state": "OPEN"}
    ]
    by_head = gh(gh_env, "pr", "list", "--head", "nope", "--json", "number", cwd=checkout)
    assert json.loads(by_head.stdout) == []
    reviewer = {**gh_env, "EVAL_HARNESS_GH_USER": "grace"}
    gh(reviewer, "pr", "review", str(number), "--approve", cwd=checkout)
    gh(gh_env, "pr", "merge", str(number), "--merge", cwd=checkout)
    git(gh_env, checkout, "pull", "-q", "origin", "main")
    assert (checkout / "semantic-gap-refunds.txt").is_file()
    pull = json.loads(gh(gh_env, "api", f"repos/{REPO}/pulls/{number}").stdout)
    assert pull["merge_commit_sha"] == git(gh_env, checkout, "rev-parse", "HEAD")
    assert pull["head"]["ref"] == "semantic/gap-refunds" and pull["merged_at"]
    assert pull["user"]["login"] == gh_standin.DEFAULT_USER
    assert pull["base"]["repo"]["full_name"] == REPO
    reviews = json.loads(
        gh(
            gh_env,
            "api",
            f"repos/{REPO}/pulls/{number}/reviews?per_page=100",
            "--paginate",
            "--slurp",
        ).stdout
    )
    assert [(r["state"], r["user"]["login"]) for r in reviews[0]] == [("APPROVED", "grace")]
    assert reviews[0][0]["commit_id"] == pull["head"]["sha"]
    query = f'repo:{REPO} is:pr is:merged in:body "gap-refunds"'
    from urllib.parse import urlencode

    pages = json.loads(
        gh(
            gh_env, "api", "search/issues?" + urlencode({"q": query}), "--paginate", "--slurp"
        ).stdout
    )
    assert [item["number"] for item in pages[0]["items"]] == [number]
    view = gh(gh_env, "pr", "view", str(number), "--json", "state,mergedAt", cwd=checkout)
    assert json.loads(view.stdout)["state"] == "MERGED"


def test_pull_requests_of_another_repository_are_not_listed(checkout, gh_env):
    open_pull(gh_env, checkout, "operate/x", "x\n")
    other = gh(gh_env, "pr", "list", "--repo", "acme/other", "--json", "number")
    assert json.loads(other.stdout) == []


def vecten(env, root, *args):
    result = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", *args, "--json"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_vecten_github_bootstrap_deploy_and_a_push_run_against_the_standin(tmp_path, gh_env):
    from vecten_shared.config import load_config

    cloud = tmp_path / "cloud"
    env = {**gh_env, **gh_standin.mock_cloud_environ(cloud, gh_env)}
    root = render_aws(tmp_path / "proj", components=["platform", "mcp"])
    vecten(env, root, "github", "init")
    boot = vecten(env, root, "bootstrap", "--env", "prod")
    assert boot["environment_branch_policy"] == "default-branch"
    vecten(env, root, "github", "push")
    deployed = vecten(env, root, "deploy", "--env", "prod")
    remote_head = git(env, root, "ls-remote", "origin", "main").split()[0]
    assert deployed["deployed_commit"] == remote_head
    assert load_config(root).environments["prod"].deployed_commit == remote_head
    assert (cloud / "hosts" / "mcp" / "current").read_text().strip() == remote_head
    outputs = json.loads((cloud / "state" / "prod" / "outputs.json").read_text())
    assert outputs["hosts"]["mcp"]["hostnames"] == ["mcp.data.acme.com"]

    pushed = vecten(env, root, "github", "push")
    assert pushed["committed"] is True
    for host in ("mcp", "platform"):
        assert (cloud / "hosts" / host / "current").read_text().strip() == pushed["commit"]
    shipped = vecten(env, root, "ship", "--env", "prod")
    assert shipped["dispatched"] is True and shipped["head_sha"] == pushed["commit"]

    logged = calls(env)
    assert all(call["answered"] for call in logged), [c for c in logged if not c["answered"]]
    assert {tuple(c["argv"][:2]) for c in logged} >= {
        ("auth", "status"),
        ("repo", "create"),
        ("repo", "view"),
        ("api", f"repos/{REPO}"),
        ("workflow", "run"),
        ("run", "list"),
        ("run", "watch"),
        ("run", "view"),
    }


def gh_calls_in_source() -> set[tuple[str, str]]:
    """Every `gh` subcommand pair vecten's source spells out, read from its call sites."""
    found = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            args = node.args
            if name == "_gh" and len(args) >= 4:
                words = args[2:4]
            elif (
                isinstance(node.func, ast.Attribute)
                and name == "run"
                and getattr(node.func.value, "id", None) == "gh"
                and args
            ):
                listed = args[0]
                words = listed.elts[:2] if isinstance(listed, ast.List) else []
            else:
                continue
            if words and all(isinstance(w, ast.Constant) for w in words[:1]):
                first = words[0].value
                second = words[1].value if isinstance(words[1], ast.Constant) else None
                found.add((first, second if first != "api" else None))
    return found


def test_the_standin_answers_every_gh_subcommand_vecten_spells(checkout, gh_env):
    spelled = gh_calls_in_source()
    assert {("workflow", "run"), ("repo", "create"), ("auth", "status"), ("api", None)} <= spelled
    assert spelled <= gh_standin.ANSWERED, spelled - gh_standin.ANSWERED
