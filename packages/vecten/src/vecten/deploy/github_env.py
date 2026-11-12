"""The project's GitHub repository as a deploy target: its environments' variables, and
dispatching a workflow and following it."""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vecten.github.gh import ToolRunner, head_commit, require_gh
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure

# GitHub stamps a run with its own clock, which may trail this machine's.
CLOCK_SKEW = timedelta(seconds=10)
RUN_LIST_LIMIT = 10
RUN_APPEAR_SECONDS = 3
RUN_APPEAR_POLLS = 40

sleep = time.sleep

_HTTP_STATUS = re.compile(r"HTTP (\d{3})")
# GitHub answers these when the repository's plan has no deployment branch policies.
_PLAN_LACKS_FEATURE = (404, 422)


def _now() -> datetime:
    return datetime.now(UTC)


def _http_status(result: subprocess.CompletedProcess) -> int | None:
    found = _HTTP_STATUS.findall(result.stderr or "")
    return int(found[-1]) if found else None


def _gh(
    gh: ToolRunner,
    cwd: Path,
    *args: str,
    what: str | None = None,
    tolerate: tuple[int, ...] = (),
    code: ExitCode = ExitCode.TOOL_FAILED,
    hint: str = "",
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    """Run `gh`, raising on failure unless GitHub answered one of the `tolerate` statuses.

    `stdin` is written to gh's standard input; secret values travel only this way, never in
    an argument, because arguments are visible to every process on the machine.
    """
    result = gh.run(list(args), cwd) if stdin is None else gh.run(list(args), cwd, input=stdin)
    if result.returncode == 0 or _http_status(result) in tolerate:
        return result
    raise Failure(
        code, f"gh {what or ' '.join(args[:2])} failed: {result.stderr.strip()[-300:]}{hint}"
    )


def _json(result: subprocess.CompletedProcess, what: str) -> Any:
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        raise Failure(ExitCode.TOOL_FAILED, f"gh {what} printed no JSON") from None


def _existing_restriction(path: str, found: subprocess.CompletedProcess) -> str:
    """`unchanged` when a branch restriction covers the environment, else `unrestricted`.

    GitHub answers `deployment_branch_policy: null` for an environment every branch may deploy
    to. Custom policies admit only the branches and tags they name, so a list naming none
    admits nothing rather than everything.
    """
    body = _json(found, f"api GET {path}")
    policy = body.get("deployment_branch_policy") if isinstance(body, dict) else None
    if not isinstance(policy, dict):
        return "unrestricted"
    if policy.get("protected_branches") or policy.get("custom_branch_policies"):
        return "unchanged"
    return "unrestricted"


def ensure_environment(
    gh: ToolRunner, repository: str, env: str, branch: str, *, cwd: Path | None = None
) -> str:
    """Create the GitHub environment `env` when absent, deployable from `branch` only.

    The environment is the deploy role's only gate: jobs that declare `environment: <env>`
    get the OIDC subject the role trusts. An existing environment is never written, so
    protection rules a human added survive a rerun. Returns `unchanged` for an existing
    environment some branch restriction covers, `unrestricted` for an existing one any branch
    may deploy to, `default-branch` once the restriction is set, and `unavailable` when the
    repository's plan offers no deployment branch policies (a private repository on a free
    plan), in which case the environment exists without the restriction.
    """
    where = cwd or Path.cwd()
    path = f"repos/{repository}/environments/{env}"
    found = _gh(gh, where, "api", path, what=f"api GET {path}", tolerate=(404,))
    if found.returncode == 0:
        return _existing_restriction(path, found)
    restricted = _gh(
        gh,
        where,
        "api",
        "-X",
        "PUT",
        path,
        "-F",
        "deployment_branch_policy[protected_branches]=false",
        "-F",
        "deployment_branch_policy[custom_branch_policies]=true",
        what=f"api PUT {path}",
        tolerate=_PLAN_LACKS_FEATURE,
    )
    if restricted.returncode != 0:
        _gh(gh, where, "api", "-X", "PUT", path, what=f"api PUT {path}")
        return "unavailable"
    policies = f"{path}/deployment-branch-policies"
    allowed = _gh(
        gh,
        where,
        "api",
        "-X",
        "POST",
        policies,
        "-f",
        f"name={branch}",
        "-f",
        "type=branch",
        what=f"api POST {policies}",
        tolerate=_PLAN_LACKS_FEATURE,
    )
    if allowed.returncode != 0:
        # Custom branch policies naming no branch would let no branch deploy at all.
        _gh(
            gh,
            where,
            "api",
            "-X",
            "PUT",
            path,
            "-F",
            "deployment_branch_policy=null",
            what=f"api PUT {path}",
        )
        return "unavailable"
    return "default-branch"


def set_environment_variables(
    gh: ToolRunner,
    repository: str,
    env: str,
    values: dict[str, str],
    *,
    cwd: Path | None = None,
) -> None:
    """Set each variable on the GitHub environment `env`, which `ensure_environment` made.

    Jobs that touch the cloud declare `environment: <env>`, which exposes these variables.
    The values are resource names, never secrets.
    """
    where = cwd or Path.cwd()
    for name, value in values.items():
        _gh(
            gh,
            where,
            "variable",
            "set",
            name,
            "--env",
            env,
            "--body",
            value,
            "--repo",
            repository,
            what=f"variable set {name} --env {env}",
        )


def set_repository_variables(
    gh: ToolRunner, repository: str, values: dict[str, str], *, cwd: Path | None = None
) -> None:
    """Set repository-level variables, which jobs that declare no environment can read."""
    where = cwd or Path.cwd()
    for name, value in values.items():
        _gh(
            gh,
            where,
            "variable",
            "set",
            name,
            "--body",
            value,
            "--repo",
            repository,
            what=f"variable set {name}",
        )


def set_environment_secrets(
    gh: ToolRunner,
    repository: str,
    env: str,
    values: dict[str, str],
    *,
    cwd: Path | None = None,
) -> None:
    """Set each secret on the GitHub environment `env`, the value on gh's standard input.

    Environment secrets reach only jobs that declare `environment: <env>`, which
    `ensure_environment` limits to the default branch.
    """
    where = cwd or Path.cwd()
    for name, value in values.items():
        _gh(
            gh,
            where,
            "secret",
            "set",
            name,
            "--env",
            env,
            "--repo",
            repository,
            what=f"secret set {name} --env {env}",
            stdin=value,
        )


def set_repository_secrets(
    gh: ToolRunner, repository: str, values: dict[str, str], *, cwd: Path | None = None
) -> None:
    """Set repository-level secrets, the value on gh's standard input; jobs that declare no
    environment, such as a pull request's plan, read these."""
    where = cwd or Path.cwd()
    for name, value in values.items():
        _gh(
            gh,
            where,
            "secret",
            "set",
            name,
            "--repo",
            repository,
            what=f"secret set {name}",
            stdin=value,
        )


def repository_facts(gh: ToolRunner, project_root: Path, repository: str) -> tuple[str, str]:
    """The repository's `owner/name` as GitHub spells it, and its default branch."""
    result = _gh(
        gh,
        project_root,
        "repo",
        "view",
        repository,
        "--json",
        "nameWithOwner,defaultBranchRef",
        what=f"repo view {repository}",
        code=ExitCode.PREREQUISITE,
        hint="; if the repository does not exist yet, run `vecten github init`",
    )
    body = _json(result, "repo view")
    if not isinstance(body, dict):
        body = {}
    name = body.get("nameWithOwner")
    branch = (body.get("defaultBranchRef") or {}).get("name")
    if not name or not branch:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"gh repo view named no repository or default branch for {repository}",
        )
    return str(name), str(branch)


def repository_ids(gh: ToolRunner, project_root: Path, repository: str) -> tuple[int, int]:
    """The numeric ids of the repository's owner and of the repository itself."""
    result = _gh(gh, project_root, "api", f"repos/{repository}", what=f"api repos/{repository}")
    body = _json(result, f"api repos/{repository}")
    try:
        return int(body["owner"]["id"]), int(body["id"])
    except KeyError, TypeError, ValueError:
        raise Failure(
            ExitCode.TOOL_FAILED, f"gh api repos/{repository} named no owner or repository id"
        ) from None


def oidc_subject_prefixes(repository: str, owner_id: int, repository_id: int) -> tuple[str, str]:
    """Both forms GitHub's OIDC `sub` claim takes for `repository`.

    Repositories created, renamed or transferred after 2026-07-15 send the immutable form,
    which names the owner's and the repository's ids; older ones send the name-only form
    until they opt in. Neither is visible through the API, so a trust names both, and the ids
    keep a repository recreated under the same name from matching the immutable form.
    """
    owner, name = repository.split("/", 1)
    return f"repo:{repository}", f"repo:{owner}@{owner_id}/{name}@{repository_id}"


def canonical_repository(configured: str, found: str) -> str:
    """GitHub's spelling of the configured repository.

    OIDC subjects compare case-sensitively against GitHub's spelling, so a repository that
    differs only by case takes GitHub's; any other difference is a different repository.
    """
    if found.lower() == configured.lower():
        return found
    raise Failure(
        ExitCode.CONFIG,
        f"GitHub answers {found!r} for the repository vecten.toml names {configured!r}; "
        "correct github_repository in vecten.toml and deploy/envs/*.tfvars",
    )


def default_branch(gh: ToolRunner, project_root: Path, repository: str) -> str:
    return repository_facts(gh, project_root, repository)[1]


SHIP_CONTEXT_PREFIX = "vecten/ship/"
SHIP_STATES = ("pending", "success", "failure")
SHIP_STATUS_DESCRIPTIONS = {
    "pending": "vecten is shipping this commit",
    "success": "every host answered its health poll",
    "failure": "the ship failed; the hosts kept or restored their previous bundle",
}
# How far back the default branch's history is searched for an environment's last ship.
SHIPPED_SEARCH_LIMIT = 200
_HISTORY_PAGE = 100
_SHIPPED_QUERY = """
query($owner: String!, $name: String!, $branch: String!, $context: String!, $first: Int!,
      $cursor: String) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: $branch) {
      target {
        ... on Commit {
          history(first: $first, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes { oid status { context(name: $context) { state } } }
          }
        }
      }
    }
  }
}
"""


def ship_context(env: str) -> str:
    return f"{SHIP_CONTEXT_PREFIX}{env}"


def set_ship_status(
    gh: ToolRunner,
    project_root: Path,
    repository: str,
    commit: str,
    env: str,
    state: str,
    *,
    target_url: str | None = None,
) -> None:
    """Set `vecten/ship/<env>` on `commit`; the job's token needs `statuses: write`."""
    if state not in SHIP_STATES:
        raise Failure(ExitCode.INTERNAL, f"{state!r} is not a ship status")
    path = f"repos/{repository}/statuses/{commit}"
    args = [
        "api",
        "-X",
        "POST",
        path,
        "-f",
        f"state={state}",
        "-f",
        f"context={ship_context(env)}",
        "-f",
        f"description={SHIP_STATUS_DESCRIPTIONS[state]}",
    ]
    if target_url:
        args += ["-f", f"target_url={target_url}"]
    _gh(
        gh,
        project_root,
        *args,
        what=f"api POST {path}",
        hint="; the job that ships needs the `statuses: write` permission",
    )


def ship_status(
    gh: ToolRunner, project_root: Path, repository: str, commit: str, env: str
) -> str | None:
    """The latest `vecten/ship/<env>` state on `commit`, or None when it carries none."""
    path = f"repos/{repository}/commits/{commit}/status"
    body = _json(_gh(gh, project_root, "api", path, what=f"api GET {path}"), f"api GET {path}")
    statuses = body.get("statuses") if isinstance(body, dict) else None
    # The combined status holds the latest status of each context only.
    for status in statuses or []:
        if isinstance(status, dict) and status.get("context") == ship_context(env):
            return str(status.get("state"))
    return None


def last_shipped(
    gh: ToolRunner, project_root: Path, repository: str, branch: str, env: str
) -> str | None:
    """The newest of `branch`'s last `SHIPPED_SEARCH_LIMIT` commits whose `vecten/ship/<env>`
    is `success`, or None.

    A ship that rolled back leaves `failure` on its commit, so the answer is then the commit
    the hosts went back to.
    """
    owner, name = repository.split("/", 1)
    cursor: str | None = None
    for start in range(0, SHIPPED_SEARCH_LIMIT, _HISTORY_PAGE):
        args = [
            "api",
            "graphql",
            "-f",
            f"query={_SHIPPED_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"branch=refs/heads/{branch}",
            "-f",
            f"context={ship_context(env)}",
            "-F",
            f"first={min(_HISTORY_PAGE, SHIPPED_SEARCH_LIMIT - start)}",
        ]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        body = _json(_gh(gh, project_root, *args, what="api graphql"), "api graphql")
        try:
            history = body["data"]["repository"]["ref"]["target"]["history"]
            nodes = history["nodes"]
            page = history["pageInfo"]
        except KeyError, TypeError:
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"gh api graphql named no history for {branch} of {repository}",
            ) from None
        for node in nodes:
            state = ((node.get("status") or {}).get("context") or {}).get("state")
            if str(state).lower() == "success":
                return str(node["oid"])
        if not page.get("hasNextPage"):
            return None
        cursor = page.get("endCursor")
    return None


def _created(run: dict) -> datetime:
    return datetime.fromisoformat(str(run["createdAt"]).replace("Z", "+00:00"))


def _dispatched_run(
    gh: ToolRunner, project_root: Path, repository: str, workflow: str, env: str, since: datetime
) -> dict:
    threshold = since - CLOCK_SKEW
    for _ in range(RUN_APPEAR_POLLS):
        result = _gh(
            gh,
            project_root,
            "run",
            "list",
            "--workflow",
            workflow,
            "--event",
            "workflow_dispatch",
            "--json",
            "databaseId,headSha,createdAt,status",
            "--limit",
            str(RUN_LIST_LIMIT),
            "--repo",
            repository,
        )
        runs = _json(result, "run list") or []
        fresh = [run for run in runs if _created(run) >= threshold]
        if fresh:
            # The earliest run after the dispatch is this dispatch's; later ones are someone else's.
            return min(fresh, key=_created)
        sleep(RUN_APPEAR_SECONDS)
    raise Failure(
        ExitCode.TOOL_FAILED,
        f"the {workflow} run dispatched for {env} did not appear within "
        f"{RUN_APPEAR_POLLS * RUN_APPEAR_SECONDS} s; see `gh run list --workflow {workflow}`",
    )


def _follow(
    gh: ToolRunner, project_root: Path, repository: str, workflow: str, run: dict, what: str
) -> tuple[str, str, str]:
    """Watch `run` to its end and return its URL, head commit and conclusion; a run that does
    not succeed raises, naming `what`."""
    run_id = run["databaseId"]
    watch = gh.run(
        ["run", "watch", str(run_id), "--exit-status", "--repo", repository], project_root
    )
    view = _gh(
        gh,
        project_root,
        "run",
        "view",
        str(run_id),
        "--json",
        "conclusion,url,headSha",
        "--repo",
        repository,
    )
    body = _json(view, "run view") or {}
    url = body.get("url", "") or run.get("url", "")
    head_sha = body.get("headSha") or run.get("headSha", "")
    conclusion = body.get("conclusion") or ("success" if watch.returncode == 0 else "failure")
    if watch.returncode != 0 or conclusion != "success":
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"{what} ended {conclusion}: {url}",
            details={"run_id": run_id, "url": url, "conclusion": conclusion},
        )
    return url, head_sha, conclusion


def follow_push_run(
    gh: ToolRunner, project_root: Path, repository: str, workflow: str, sha: str
) -> dict:
    """Wait for the run a push of `sha` started for `workflow` and follow it to its end."""
    for _ in range(RUN_APPEAR_POLLS):
        result = _gh(
            gh,
            project_root,
            "run",
            "list",
            "--workflow",
            workflow,
            "--commit",
            sha,
            "--json",
            "databaseId,url",
            "--limit",
            "1",
            "--repo",
            repository,
        )
        runs = _json(result, "run list") or []
        if runs:
            url, _head, conclusion = _follow(
                gh, project_root, repository, workflow, runs[0], f"{workflow} for {sha[:8]}"
            )
            return {"run_url": url, "conclusion": conclusion}
        sleep(RUN_APPEAR_SECONDS)
    raise Failure(
        ExitCode.TOOL_FAILED,
        f"no {workflow} run for {sha[:8]} appeared within "
        f"{RUN_APPEAR_POLLS * RUN_APPEAR_SECONDS} s; see `gh run list --workflow {workflow}`",
    )


def dispatch_and_follow(
    project_root: Path,
    workflow: str,
    env: str,
    gh: ToolRunner,
    git: ToolRunner,
    *,
    commit: str | None = None,
) -> dict:
    """Run `workflow` on the default branch for `env` and wait for its conclusion.

    The workflow always runs the default branch as GitHub holds it. With `commit` it ships
    that commit, which the result names as `commit`; the run's `head_sha` stays the default
    branch's. The result's `matches_local_head` says whether the run's head is this
    checkout's HEAD.
    """
    config = load_config(project_root)
    if config.deploy is None:
        raise Failure(
            ExitCode.REFUSED, "no GitHub repository recorded; run `vecten init --deploy` first"
        )
    repository = config.deploy.github_repository
    require_gh(gh, project_root)
    branch = default_branch(gh, project_root, repository)
    since = _now()
    _gh(
        gh,
        project_root,
        "workflow",
        "run",
        workflow,
        "--ref",
        branch,
        "-f",
        f"environment={env}",
        *(["-f", f"commit={commit}"] if commit else []),
        "--repo",
        repository,
    )
    run = _dispatched_run(gh, project_root, repository, workflow, env, since)
    run_id = run["databaseId"]
    url, head_sha, conclusion = _follow(
        gh, project_root, repository, workflow, run, f"{workflow} for {env}"
    )
    followed = {
        "run_id": run_id,
        "head_sha": head_sha,
        "conclusion": conclusion,
        "url": url,
        "branch": branch,
        "matches_local_head": head_sha == head_commit(git, project_root),
    }
    return {**followed, "commit": commit} if commit else followed
