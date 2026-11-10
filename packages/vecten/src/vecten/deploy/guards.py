"""Where a deploy command runs and what the committed facts allow it to do."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from vecten.deploy.github_env import default_branch
from vecten.deploy.terraform import redacted_tail
from vecten.github.gh import ToolRunner, require_gh
from vecten_shared.config import EnvironmentFacts, VectenConfig
from vecten_shared.output import ExitCode, Failure

# CI reads these from GitHub, not from this checkout.
DEPLOY_FILES = ("vecten.toml", "deploy")
PUSH_HINT = "`vecten github push`"


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def facts(config: VectenConfig, env: str) -> EnvironmentFacts:
    # `environments` adds a missing key on lookup, so reads go through `get`.
    return config.environments.get(env) or EnvironmentFacts()


def is_bootstrapped(config: VectenConfig, env: str) -> bool:
    return facts(config, env).bootstrapped


def is_deployed(config: VectenConfig, env: str) -> bool:
    return facts(config, env).deployed_commit is not None


def require_bootstrapped(config: VectenConfig, env: str) -> str:
    """The environment's state bucket, refusing an environment `bootstrap` has not prepared."""
    found = facts(config, env)
    if not found.bootstrapped or not found.state_bucket:
        raise Failure(
            ExitCode.REFUSED,
            f"environment {env!r} is not bootstrapped; run `vecten bootstrap --env {env}`",
        )
    return found.state_bucket


def _git_failure(args: list[str], result: subprocess.CompletedProcess) -> Failure:
    # git prints remote URLs, which can carry a token, so only the filtered tail is shown.
    tail = "; ".join(redacted_tail(result.stderr, 5))
    message = f"git {' '.join(args)} failed (exit {result.returncode})"
    return Failure(ExitCode.TOOL_FAILED, message + (f": {tail}" if tail else ""))


def _git(git: ToolRunner, project_root: Path, *args: str) -> str:
    result = git.run(list(args), project_root)
    if result.returncode != 0:
        raise _git_failure(list(args), result)
    return result.stdout


def require_on_default_branch(
    project_root: Path, repository: str, gh: ToolRunner, git: ToolRunner
) -> str:
    """The default branch, refusing a deploy GitHub would not run as this checkout holds it.

    The dispatched workflow checks out the default branch and reads `vecten.toml` and
    `deploy/` from it, so local edits or unpushed commits to those would deploy stale
    tfvars or be refused for a fact GitHub never saw. Those are checked before ancestry, so
    an unpushed commit on the default branch itself is answered with the push command.
    """
    require_gh(gh, project_root)
    branch = default_branch(gh, project_root, repository)
    _git(git, project_root, "fetch", "origin", branch)
    if _git(git, project_root, "status", "--porcelain", "--", *DEPLOY_FILES).strip():
        raise Failure(
            ExitCode.REFUSED,
            f"vecten.toml or deploy/ has uncommitted changes the {branch} workflow would not "
            f"see; commit and push them to {branch} with {PUSH_HINT} first",
        )
    unpushed = _git(git, project_root, "rev-list", "FETCH_HEAD..HEAD", "--", *DEPLOY_FILES)
    if unpushed.strip():
        raise Failure(
            ExitCode.REFUSED,
            f"vecten.toml or deploy/ has commits that are not on origin/{branch}; the "
            f"workflow reads them from GitHub, so push them to {branch} with {PUSH_HINT} first",
        )
    ancestry_args = ["merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"]
    ancestry = git.run(ancestry_args, project_root)
    # `--is-ancestor` exits 1 for "not an ancestor"; any other non-zero exit is an error.
    if ancestry.returncode == 1:
        raise Failure(
            ExitCode.REFUSED,
            f"the checked-out commit is not on origin/{branch}; deploy runs only what is on "
            f"{branch}, so merge or push it to {branch} ({PUSH_HINT} from {branch}) first",
        )
    if ancestry.returncode != 0:
        raise _git_failure(ancestry_args, ancestry)
    return branch


def require_deployed(config: VectenConfig, env: str) -> None:
    if not is_deployed(config, env):
        raise Failure(
            ExitCode.REFUSED,
            f"environment {env!r} is not deployed; run `vecten deploy --env {env}` first",
        )
