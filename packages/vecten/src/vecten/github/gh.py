from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from vecten.cli.injection import load_runner
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure

# The ship workflow `init` renders runs on pushes to this branch only.
SHIP_BRANCH = "main"
SHIP_WORKFLOW = "ship.yml"


class ToolRunner(Protocol):
    def which(self) -> str | None: ...

    # `input` is passed only by callers that write to the tool's standard input.
    def run(
        self, args: list[str], cwd: Path, input: str | None = None
    ) -> subprocess.CompletedProcess: ...


class SubprocessTool:
    def __init__(self, binary: str):
        self.binary = binary

    def which(self) -> str | None:
        return shutil.which(self.binary)

    def run(
        self, args: list[str], cwd: Path, input: str | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.binary, *args], cwd=cwd, capture_output=True, text=True, input=input
        )


def get_gh() -> ToolRunner:
    return load_runner("VECTEN_GH_RUNNER", lambda: SubprocessTool("gh"))


def get_git() -> ToolRunner:
    return load_runner("VECTEN_GIT_RUNNER", lambda: SubprocessTool("git"))


def require_gh(gh: ToolRunner, cwd: Path) -> None:
    if gh.which() is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            "the GitHub CLI `gh` is not installed; install it, then run `gh auth login`",
        )
    status = gh.run(["auth", "status"], cwd)
    if status.returncode != 0:
        raise Failure(
            ExitCode.PREREQUISITE,
            "the GitHub CLI is not logged in; run `gh auth login` "
            f"({status.stderr.strip()[-300:]})",
        )


def _git(git: ToolRunner, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    r = git.run(list(args), cwd)
    if r.returncode != 0:
        raise Failure(
            ExitCode.TOOL_FAILED, f"git {' '.join(args)} failed: {r.stderr.strip()[-500:]}"
        )
    return r


def head_commit(git: ToolRunner, cwd: Path) -> str | None:
    r = git.run(["rev-parse", "HEAD"], cwd)
    return r.stdout.strip() if r.returncode == 0 else None


def _commit_pending(git: ToolRunner, cwd: Path, message: str) -> bool:
    _git(git, cwd, "add", "-A")
    if not _git(git, cwd, "status", "--porcelain").stdout.strip():
        return False
    _git(git, cwd, "commit", "-m", message)
    return True


def _require_own_repository(git: ToolRunner, project_root: Path) -> None:
    # Inside an enclosing repository `git add -A` would stage that repository's whole tree.
    top = git.run(["rev-parse", "--show-toplevel"], project_root)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != project_root.resolve():
        raise Failure(
            ExitCode.REFUSED,
            f"{project_root} is not its own git repository; run `vecten github init` first",
        )


def github_init(project_root: Path, gh: ToolRunner, git: ToolRunner) -> dict:
    config = load_config(project_root)
    if config.deploy is None:
        raise Failure(
            ExitCode.REFUSED,
            "no GitHub repository recorded; answer the deploy questions and run "
            "`vecten init --deploy`",
        )
    require_gh(gh, project_root)
    if (project_root / ".git").exists():
        # `gh repo create --remote origin` creates the GitHub repository before it fails on an
        # existing remote, leaving an orphan repository that a retry cannot adopt.
        origin = git.run(["remote", "get-url", "origin"], project_root)
        if origin.returncode == 0:
            raise Failure(
                ExitCode.REFUSED,
                f"this project already has an origin remote ({origin.stdout.strip()}); "
                "run `vecten github push`",
            )
    else:
        _git(git, project_root, "init", "-b", "main")
    committed = _commit_pending(git, project_root, "vecten init")
    repo = config.deploy.github_repository
    r = gh.run(
        ["repo", "create", repo, "--private", "--source", ".", "--remote", "origin", "--push"],
        project_root,
    )
    if r.returncode != 0:
        raise Failure(ExitCode.TOOL_FAILED, f"gh repo create failed: {r.stderr.strip()[-500:]}")
    return {
        "repository": repo,
        "pushed": True,
        "committed": committed,
        "commit": head_commit(git, project_root),
    }


def _remote_head(git: ToolRunner, project_root: Path, branch: str) -> str | None:
    r = git.run(["rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"], project_root)
    return r.stdout.strip() if r.returncode == 0 else None


def github_push(
    project_root: Path, gh: ToolRunner, git: ToolRunner, message: str, *, wait: bool = True
) -> dict:
    """Commit and push; on a deployed project, follow the ship the push starts, since admin
    commands that follow reach the MCP and only the shipped bundle carries `[mcp]` changes."""
    config = load_config(project_root)
    require_gh(gh, project_root)
    _require_own_repository(git, project_root)
    committed = _commit_pending(git, project_root, message)
    branch = _git(git, project_root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    before = _remote_head(git, project_root, branch)
    _git(git, project_root, "push", "-u", "origin", "HEAD")
    commit = head_commit(git, project_root)
    repo = config.deploy.github_repository if config.deploy else None
    deployed = any(facts.deployed_commit for facts in config.environments.values())
    ship = None
    # The ship workflow runs on pushes to main only, and a push that moved nothing starts none.
    if wait and repo and deployed and branch == SHIP_BRANCH and commit and commit != before:
        from vecten.deploy.github_env import follow_push_run

        ship = follow_push_run(gh, project_root, repo, SHIP_WORKFLOW, commit)
    return {
        "repository": repo,
        "pushed": True,
        "committed": committed,
        "commit": commit,
        "ship": ship,
    }
