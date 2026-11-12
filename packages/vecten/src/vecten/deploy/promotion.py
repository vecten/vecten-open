"""The promotion order as deploy commands read it, what each environment shipped last, and
the checkout a promoted commit ships from.

`vecten_shared.config` checks the order's shape for every reader of `vecten.toml`; the tfvars
of each environment and the release that rendered the workflows exist only in a project
checkout, so the commands that act on the order check them here.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vecten.deploy.bundle import head_commit
from vecten.deploy.github_env import (
    SHIPPED_SEARCH_LIMIT,
    last_shipped,
    ship_context,
    ship_status,
)
from vecten.deploy.guards import _git
from vecten.deploy.tfvars import env_tfvars_path
from vecten.github.gh import ToolRunner
from vecten_shared.config import (
    CONFIG_FILE,
    VectenConfig,
    load_config,
    promotion_scaffold_problem,
    save_config,
)
from vecten_shared.output import ExitCode, Failure

__all__ = [
    "SHIPPED_SEARCH_LIMIT",
    "fetch_default_branch",
    "given_commit",
    "newest_shipped",
    "promoted_tree",
    "promotion_order",
    "require_shipped_by",
    "ship_context",
]

# A commit id a person types, abbreviated or full; nothing git would read as an option.
_COMMIT_REF = re.compile(r"[0-9a-f]{7,40}")
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")


def promotion_order(project_root: Path, config: VectenConfig) -> tuple[str, ...]:
    """The order, refusing one an older workflow would ignore or that names an environment
    without `deploy/envs/<env>.tfvars`."""
    order = config.promotion_order()
    if config.deploy is None or config.deploy.promotion is None:
        return order
    problem = promotion_scaffold_problem(config)
    if problem:
        raise Failure(ExitCode.REFUSED, problem)
    for env in order:
        if not env_tfvars_path(project_root, env).is_file():
            raise Failure(
                ExitCode.CONFIG,
                f"{project_root / CONFIG_FILE}: deploy.promotion: {env} has no "
                f"deploy/envs/{env}.tfvars",
            )
    return order


def newest_shipped(
    gh: ToolRunner, project_root: Path, repository: str, branch: str, env: str
) -> str:
    """The commit `env` shipped last, refusing when none of the searched commits carries it."""
    found = last_shipped(gh, project_root, repository, branch, env)
    if found is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"{env} shipped none of the last {SHIPPED_SEARCH_LIMIT} commits on {branch} (no "
            f"{ship_context(env)} success status); ship {env} first, or name a commit {env} "
            "shipped with --commit",
        )
    return found


def fetch_default_branch(git: ToolRunner, project_root: Path, branch: str) -> None:
    """Fetch the remote default branch, so a commit named on it is in this checkout."""
    _git(git, project_root, "fetch", "origin", branch)


def given_commit(git: ToolRunner, project_root: Path, ref: str) -> str:
    """The full id of the commit `ref`, an abbreviated or full commit id, names here."""
    if not _COMMIT_REF.fullmatch(ref):
        raise Failure(
            ExitCode.USAGE, f"--commit takes a commit id of 7 to 40 hex digits; got {ref!r}"
        )
    result = git.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], project_root)
    commit = result.stdout.strip()
    if result.returncode != 0 or not _FULL_COMMIT.fullmatch(commit):
        raise Failure(
            ExitCode.REFUSED,
            f"commit {ref} is not in this checkout; fetch the default branch or name a commit "
            "it holds",
        )
    return commit


def require_shipped_by(
    gh: ToolRunner,
    project_root: Path,
    repository: str,
    commit: str,
    env: str,
    previous: str,
) -> None:
    """Refuse `commit` for `env` unless `previous`, the environment before it, shipped it."""
    state = ship_status(gh, project_root, repository, commit, previous)
    if state != "success":
        found = f"is {state}" if state else "is missing"
        raise Failure(
            ExitCode.REFUSED,
            f"{previous} has not shipped commit {commit[:8]} (its {ship_context(previous)} "
            f"status {found}); {env} ships only a commit {previous} shipped",
        )


@contextmanager
def promoted_tree(
    git: ToolRunner, project_root: Path, commit: str, head: VectenConfig
) -> Iterator[Path]:
    """A checkout of `commit` whose `vecten.toml` holds the environment facts of `head`.

    The content, tfvars and Terraform roots come from the promoted commit, but the facts the
    CLI records describe the environments as they are now, which only the default branch's
    head knows. The checked-out commit itself ships from where it stands.
    """
    if commit == head_commit(git, project_root):
        yield project_root
        return
    with tempfile.TemporaryDirectory(prefix="vecten-promoted-") as tmp:
        tree = Path(tmp) / "tree"
        _git(git, project_root, "worktree", "add", "--detach", str(tree), commit)
        try:
            config = load_config(tree)
            config.environments.clear()
            config.environments.update(
                {env: facts.model_copy(deep=True) for env, facts in head.environments.items()}
            )
            save_config(tree, config)
            yield tree
        finally:
            git.run(["worktree", "remove", "--force", str(tree)], project_root)
            git.run(["worktree", "prune"], project_root)
