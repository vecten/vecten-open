"""The promotion order as deploy commands read it, what each environment shipped last, and
the checkout a promoted commit ships from.

`vecten_shared.config` checks the order's shape for every reader of `vecten.toml`; the tfvars
of each environment and the release that rendered the workflows exist only in a project
checkout, so the commands that act on the order check them here.
"""

from __future__ import annotations

import re
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

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
    DEFAULT_PROMOTION,
    EnvironmentFacts,
    VectenConfig,
    load_config,
    promotion_problem,
    promotion_scaffold_problem,
    save_config,
)
from vecten_shared.names import env_name_problem
from vecten_shared.output import ExitCode, Failure

__all__ = [
    "SHIPPED_SEARCH_LIMIT",
    "HeadFacts",
    "previous_in",
    "read_head_facts",
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


def previous_in(order: tuple[str, ...], env: str) -> str | None:
    """The environment `env` promotes from in `order`; None for the first or one outside it."""
    if env not in order:
        return None
    index = order.index(env)
    return order[index - 1] if index else None


@dataclass
class HeadFacts:
    """What a promoted run takes from the default branch's head: the order and the facts."""

    order: tuple[str, ...]
    environments: dict[str, EnvironmentFacts]
    warnings: list[str] = field(default_factory=list)


def read_head_facts(project_root: Path) -> HeadFacts:
    """The head's promotion order and `[environments.*]` facts, read leniently.

    A promoted run's CLI is the release the promoted commit pins, which may be older than the
    one that wrote the head's `vecten.toml`. It takes only what it knows from the head, names
    every fact it does not, and never parses the rest of that file.
    """
    path = project_root / CONFIG_FILE
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise Failure(ExitCode.CONFIG, f"{path}: {exc}") from None
    deploy = raw.get("deploy") if isinstance(raw.get("deploy"), dict) else {}
    declared = deploy.get("promotion")
    order = DEFAULT_PROMOTION
    if declared is not None:
        if not isinstance(declared, list) or not all(isinstance(e, str) for e in declared):
            raise Failure(ExitCode.CONFIG, f"{path}: deploy.promotion: a list of environments")
        problem = promotion_problem(declared)
        if problem:
            raise Failure(ExitCode.CONFIG, f"{path}: deploy.promotion: {problem}")
        order = tuple(declared)
    known = set(EnvironmentFacts.model_fields)
    tables = raw.get("environments") if isinstance(raw.get("environments"), dict) else {}
    environments: dict[str, EnvironmentFacts] = {}
    warnings: list[str] = []
    for env, table in tables.items():
        if env_name_problem(env) or not isinstance(table, dict):
            raise Failure(ExitCode.CONFIG, f"{path}: environments.{env} is no environment's facts")
        unknown = sorted(set(table) - known)
        if unknown:
            warnings.append(
                f"{CONFIG_FILE} [environments.{env}] sets {', '.join(unknown)}, which this "
                "vecten does not know; the promoted run went on without them"
            )
        try:
            environments[env] = EnvironmentFacts.model_validate(
                {key: value for key, value in table.items() if key in known}
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"])
            raise Failure(
                ExitCode.CONFIG, f"{path}: environments.{env}.{where}: {first['msg']}"
            ) from None
    return HeadFacts(order, environments, warnings)


@contextmanager
def promoted_tree(
    git: ToolRunner, project_root: Path, commit: str, facts: dict[str, EnvironmentFacts]
) -> Iterator[Path]:
    """A checkout of `commit` whose `vecten.toml` holds `facts`, the head's environment facts.

    The content, tfvars and Terraform roots come from the promoted commit, but the facts the
    CLI records describe the environments as they are now, which only the default branch's
    head knows.
    """
    with tempfile.TemporaryDirectory(prefix="vecten-promoted-") as tmp:
        tree = Path(tmp) / "tree"
        _git(git, project_root, "worktree", "add", "--detach", str(tree), commit)
        try:
            config = load_config(tree)
            config.environments.clear()
            config.environments.update(
                {env: found.model_copy(deep=True) for env, found in facts.items()}
            )
            save_config(tree, config)
            yield tree
        finally:
            git.run(["worktree", "remove", "--force", str(tree)], project_root)
            git.run(["worktree", "prune"], project_root)
