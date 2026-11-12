"""The promotion order as deploy commands read it, and what each environment shipped last.

`vecten_shared.config` checks the order's shape for every reader of `vecten.toml`; the tfvars
of each environment and the release that rendered the workflows exist only in a project
checkout, so the commands that act on the order check them here.
"""

from __future__ import annotations

from pathlib import Path

from vecten.deploy.github_env import SHIPPED_SEARCH_LIMIT, last_shipped, ship_context
from vecten.deploy.tfvars import env_tfvars_path
from vecten.github.gh import ToolRunner
from vecten_shared.config import CONFIG_FILE, VectenConfig, promotion_scaffold_problem
from vecten_shared.output import ExitCode, Failure

__all__ = [
    "SHIPPED_SEARCH_LIMIT",
    "newest_shipped",
    "promotion_order",
    "ship_context",
]


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
