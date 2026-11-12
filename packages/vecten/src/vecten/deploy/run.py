"""The deploy commands' bodies: what runs inside GitHub Actions and what the laptop dispatches.

Everything cloud-specific goes through `vecten.deploy.control.ControlPlane`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from vecten.deploy.bundle import check_commit, head_commit
from vecten.deploy.control import (
    ControlPlane,
    destroy_environment,
    get_control_plane,
    plan_environment,
    provision_environment,
)
from vecten.deploy.expiry import (
    RETIRING_PREFIX,
    VM_KEY_OWNERS,
    expiring_keys,
    renewal_warning,
)
from vecten.deploy.github_env import (
    canonical_repository,
    configured_names,
    dispatch_and_follow,
    ensure_environment,
    oidc_subject_prefixes,
    repository_facts,
    repository_ids,
    set_environment_secrets,
    set_environment_variables,
    set_repository_secrets,
    set_repository_variables,
)
from vecten.deploy.guards import (
    in_actions,
    is_bootstrapped,
    is_deployed,
    require_bootstrapped,
    require_deployed,
    require_on_default_branch,
)
from vecten.deploy.shipping import service_report
from vecten.deploy.snowflake import has_snowflake_root, identity_pending, identity_pending_note
from vecten.deploy.terraform import PlannedRoot, TerraformRunner, require_terraform
from vecten.deploy.tfvars import check_tfvars
from vecten.github.gh import ToolRunner, require_gh
from vecten_shared.config import DeploySection, VectenConfig, load_config, save_config
from vecten_shared.declarations import check_settings
from vecten_shared.extension_record import read_record as read_extension_record
from vecten_shared.names import check_env_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import EnvironmentOutputs, environment_file
from vecten_shared.records import write_text_atomic
from vecten_shared.services import check_bindings, limits_per_host

SHIP_WORKFLOW = "deploy-platform.yml"
APPLY_WORKFLOW = "terraform-apply.yml"
RUNBOOK = "docs/runbook.md"
RUNBOOK_PROTECT_SECTION = '"Protect the deploy environment"'
GITHUB_OUTPUT_VAR = "GITHUB_OUTPUT"


GIT_SECRET = "VECTEN_GIT_CREDENTIALS"
GIT_APP_VARIABLE = "VECTEN_GIT_APP_ID"
# The rendered workflows mint an App token only when all of these are set, so the App reaches
# only the repositories named, never every repository it is installed on.
GIT_APP_NAMES = (
    GIT_APP_VARIABLE,
    "VECTEN_GIT_APP_PRIVATE_KEY",
    "VECTEN_GIT_APP_OWNER",
    "VECTEN_GIT_APP_REPOSITORIES",
)


def require_ci_git_credential(
    project_root: Path, config: VectenConfig, env: str, gh: ToolRunner
) -> list[str]:
    """Refuse to dispatch a ship of a private git source that CI holds no credential for; the
    warnings to show when GitHub would not list every name.

    Only names are compared: a secret's value cannot be read, and a wrong one fails the ship in
    CI before any host changes."""
    from vecten.git_extensions import registered_sources

    record = read_extension_record(project_root) or {}
    private = [
        name
        for name, (_, entry) in registered_sources(config, record).items()
        if not entry.git.public
    ]
    if not private:
        return []
    repository = require_deploy_cloud(config).github_repository
    names, unread = configured_names(gh, project_root, repository, env)
    app_missing = [name for name in GIT_APP_NAMES if name not in names]
    if GIT_SECRET in names:
        if GIT_APP_VARIABLE in names and app_missing:
            return [
                f"the GitHub App for git sources lacks {', '.join(app_missing)}, so CI "
                f"fetches with {GIT_SECRET} alone"
            ]
        return []
    if GIT_APP_VARIABLE in names and not app_missing:
        return []
    if unread:
        # CI refuses a missing credential itself, before any host changes.
        return [
            f"{', '.join(unread)} needs admin rights on {repository}, so whether CI holds "
            f"{GIT_SECRET} or a GitHub App for {', '.join(private)} was not checked"
        ]
    if GIT_APP_VARIABLE in names:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"{', '.join(private)} comes from a private git repository, and {repository} sets "
            f"{GIT_APP_VARIABLE} for it but lacks {', '.join(app_missing)}; set them, as "
            "docs/extensions-private-git.md describes",
        )
    raise Failure(
        ExitCode.PREREQUISITE,
        f"{', '.join(private)} comes from a private git repository, and CI fetches it on every "
        f"ship, but {repository} and its environment {env} have neither the secret "
        f"{GIT_SECRET} nor the variable {GIT_APP_VARIABLE}; set {GIT_SECRET} "
        f"(`gh secret set {GIT_SECRET} --env {env}`), or a GitHub App with "
        f"{', '.join(GIT_APP_NAMES)}, as docs/extensions-private-git.md describes",
    )


def check_facts(config: VectenConfig, env: str) -> dict[str, bool]:
    return {"bootstrapped": is_bootstrapped(config, env), "deployed": is_deployed(config, env)}


def export_facts(found: dict[str, bool]) -> None:
    """Append `name=true|false` lines to the step's output file when Actions provides one."""
    path = os.environ.get(GITHUB_OUTPUT_VAR)
    if not path:
        return
    # The runner reads this file after the step ends, and earlier commands in the same step
    # may have written their own lines to it, so it is appended to, never replaced.
    with open(path, "a", encoding="utf-8") as handle:
        for name, value in found.items():
            handle.write(f"{name}={'true' if value else 'false'}\n")


def require_deploy_cloud(config: VectenConfig) -> DeploySection:
    """The project's deploy answers, refusing a project without a deploy layer."""
    if config.deploy is None:
        raise Failure(
            ExitCode.REFUSED, "this project has no deploy layer; run `vecten init --deploy` first"
        )
    return config.deploy


def control_plane(config: VectenConfig, project_root: Path, env: str) -> ControlPlane:
    """The control plane of the project's cloud, refusing a cloud this release cannot deploy."""
    return get_control_plane(require_deploy_cloud(config).cloud, project_root, env)


def fetch_outputs(plane: ControlPlane) -> EnvironmentOutputs:
    """Read the outputs `deploy` stored in the state store and write them into the project."""
    outputs = plane.stored_outputs()
    if outputs is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"{plane.outputs_location()} does not exist; run `vecten deploy --env {plane.env}`",
        )
    return outputs


def current_outputs(plane: ControlPlane) -> EnvironmentOutputs | None:
    """The environment's outputs: the state store's copy, which every apply rewrites, else
    the project's file; None when neither exists."""
    outputs = plane.stored_outputs()
    if outputs is None and environment_file(plane.project_root, plane.env).is_file():
        outputs = plane.outputs()
    return outputs


def bootstrap_env(project_root: Path, env: str, *, gh: ToolRunner) -> dict:
    """Prepare `env` for CI from the laptop: the cloud's state store and CI identities, then
    the GitHub environment, its variables and secrets.

    Every check that needs no cloud write runs first, `gh` and the repository lookup
    included, so a refusal leaves the cloud untouched. The fact is recorded only after the
    GitHub variables are set, because the workflows read them and `next` stops offering
    bootstrap once the fact is true.
    """
    check_env_name(env)
    config = load_config(project_root)
    deploy = require_deploy_cloud(config)
    plane = get_control_plane(deploy.cloud, project_root, env)
    configured = deploy.github_repository
    values = check_tfvars(project_root, config, env)
    if values["github_repository"] != configured:
        raise Failure(
            ExitCode.CONFIG,
            f"deploy/envs/{env}.tfvars sets github_repository = "
            f"{values['github_repository']!r}; vecten.toml says {configured!r}",
        )
    account = plane.check_login()
    require_gh(gh, project_root)
    found_name, branch = repository_facts(gh, project_root, configured)
    repository = canonical_repository(configured, found_name)
    prefixes = oidc_subject_prefixes(repository, *repository_ids(gh, project_root, repository))
    prepared = plane.bootstrap_env(env, repository, branch, account, subject_prefixes=prefixes)
    env_variables = prepared.environment_variables
    repo_variables = prepared.repository_variables
    branch_policy = ensure_environment(gh, repository, env, branch, cwd=project_root)
    set_environment_variables(gh, repository, env, env_variables, cwd=project_root)
    set_environment_secrets(gh, repository, env, prepared.environment_secrets, cwd=project_root)
    set_repository_variables(gh, repository, repo_variables, cwd=project_root)
    set_repository_secrets(gh, repository, prepared.repository_secrets, cwd=project_root)
    facts = config.environments[env]
    facts.bootstrapped = True
    facts.state_bucket = prepared.state_bucket
    if prepared.cloud_ids is not None:
        facts.cloud_ids = dict(prepared.cloud_ids)
    save_config(project_root, config)
    data = {
        "env": env,
        "repository": repository,
        "state_bucket": prepared.state_bucket,
        **prepared.report,
        "environment_branch_policy": branch_policy,
        "environment_variables": list(env_variables),
        "repository_variables": list(repo_variables),
    }
    unprotected = {
        "unavailable": "this repository's plan offers no deployment branch policies",
        "unrestricted": "it existed without a deployment branch restriction, and vecten never "
        "changes an existing environment",
    }
    if branch_policy in unprotected:
        data["warnings"] = [
            f"GitHub environment {env} accepts deployments from any branch: "
            f"{unprotected[branch_policy]}. Protect it by hand; see "
            f"{RUNBOOK} ({RUNBOOK_PROTECT_SECTION})"
        ]
    if repository != configured and prepared.repository_use:
        data["repository_note"] = (
            f"GitHub spells the repository {repository!r}; vecten.toml says {configured!r}. "
            f"The {prepared.repository_use} uses {repository!r}"
        )
    return data


def plan_path(project_root: Path, env: str) -> Path:
    return project_root / ".vecten" / "plans" / f"{env}.txt"


def write_plan_text(project_root: Path, env: str, plans: list[PlannedRoot]) -> Path:
    """Write the human-readable plan of every root, in plan order, to `plan_path`."""
    sections = []
    for planned in plans:
        where = planned.root.root_dir.relative_to(project_root).as_posix()
        sections.append(f"Terraform root {where}:\n\n{planned.text.rstrip()}\n")
    path = plan_path(project_root, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, "\n".join(sections))
    return path


def _plan_only(
    plane: ControlPlane,
    project_root: Path,
    env: str,
    terraform: TerraformRunner,
    *,
    cloud_only: bool,
) -> dict:
    require_terraform(terraform)
    if not in_actions():
        # The laptop plans with the human's own login; a missing or expired one is refused
        # here rather than surfacing as a Terraform backend error.
        plane.check_login()
    with tempfile.TemporaryDirectory(prefix="vecten-plan-") as tmp:
        plans = plan_environment(plane, env, terraform, Path(tmp), cloud_only=cloud_only)
        path = write_plan_text(project_root, env, plans)
    return {
        "env": env,
        "plan_only": True,
        "dispatched": False,
        "roots": [p.root.name for p in plans],
        "plan_file": str(path),
    }


def deploy_env(
    project_root: Path,
    env: str,
    *,
    plan_only: bool,
    check: bool,
    gh: ToolRunner | None,
    git: ToolRunner,
    terraform: TerraformRunner,
    cloud_only: bool = False,
) -> dict:
    """Inside Actions, plan and apply the environment's roots, then ship every host; on the
    laptop, dispatch the apply workflow, follow it and record the commit it deployed.

    `cloud_only` plans the cloud root alone, for the pull-request plan, whose role reads no
    deploy-scope secret. Every refusal that needs no cloud runs before the first Terraform
    call or dispatch.
    """
    check_env_name(env)
    if cloud_only and (check or not plan_only):
        # An apply that skipped the Snowflake root would leave the environment half deployed.
        raise Failure(
            ExitCode.USAGE,
            "--cloud-only plans the cloud root alone; use it with --plan-only and without --check",
        )
    config = load_config(project_root)
    if check:
        found = check_facts(config, env)
        export_facts(found)
        return found
    deploy = require_deploy_cloud(config)
    plane = get_control_plane(deploy.cloud, project_root, env)
    check_tfvars(project_root, config, env)
    require_bootstrapped(config, env)
    data = _deploy(
        plane, project_root, config, env, plan_only, gh, git, terraform, cloud_only=cloud_only
    )
    # An expiring key is warned of, never refused: the deploy that warns still works.
    expiring = expiring_keys(config.environments.get(env), datetime.now(UTC))
    if expiring:
        data["warnings"] = [*data.get("warnings", []), renewal_warning(env, expiring)]
    return data


def _deploy(
    plane: ControlPlane,
    project_root: Path,
    config: VectenConfig,
    env: str,
    plan_only: bool,
    gh: ToolRunner | None,
    git: ToolRunner,
    terraform: TerraformRunner,
    *,
    cloud_only: bool,
) -> dict:
    deploy = require_deploy_cloud(config)
    if plan_only:
        return _plan_only(plane, project_root, env, terraform, cloud_only=cloud_only)
    if not in_actions():
        if gh is None:
            raise Failure(ExitCode.INTERNAL, "dispatching the apply workflow needs the gh runner")
        require_on_default_branch(project_root, deploy.github_repository, gh, git)
        warnings = require_ci_git_credential(project_root, config, env, gh)
        followed = dispatch_and_follow(project_root, APPLY_WORKFLOW, env, gh, git)
        try:
            deployed = check_commit(followed["head_sha"])
        except Failure:
            # `next` and the workflow gates read any non-empty value as deployed.
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"{APPLY_WORKFLOW} run {followed['run_id']} succeeded but GitHub named no head "
                f"commit, so deployed_commit was not recorded: {followed['url']}",
            ) from None
        # The run took minutes; vecten.toml is read again so edits made meanwhile survive.
        recorded = load_config(project_root)
        facts = recorded.environments[env]
        facts.deployed_commit = deployed
        # A deploy hands every VM its current key and waits for the acknowledgement, so no VM
        # key is waiting any more; bootstrap deletes the old keys on its next run.
        facts.cloud_ids = {
            key: value
            for key, value in facts.cloud_ids.items()
            if key not in {f"{RETIRING_PREFIX}{host}" for host in VM_KEY_OWNERS}
        }
        save_config(project_root, recorded)
        result = {
            "env": env,
            "plan_only": False,
            "dispatched": True,
            **followed,
            "deployed_commit": deployed,
        }
        if identity_pending(recorded, env):
            result["ship_skipped"] = identity_pending_note(env)
        if warnings:
            result["warnings"] = warnings
        return result
    require_terraform(terraform)
    commit = head_commit(git, project_root)
    provisioned = provision_environment(plane, env, terraform)
    outputs = EnvironmentOutputs.model_validate(provisioned["outputs"])
    # CI never writes vecten.toml: the laptop that dispatched this run records the commit.
    data = {
        "env": env,
        "plan_only": False,
        "dispatched": False,
        "roots": provisioned["roots"],
        "commit": commit,
    }
    if identity_pending(config, env):
        return {**data, "hosts": [], "ship_skipped": identity_pending_note(env)}
    hosts = plane.ship_hosts(project_root, config, env, outputs, commit)
    report = _services_after_ship(plane, project_root, config, outputs)
    return {**data, "hosts": hosts, **report}


def _services_after_ship(
    plane: ControlPlane, project_root: Path, config: VectenConfig, outputs: EnvironmentOutputs
) -> dict:
    """What the ship found about services: their states, the routes waiting for a deploy and
    the volumes of services each host no longer runs."""
    report = service_report(config, read_extension_record(project_root) or {}, outputs)
    orphaned = {host: names for host, names in plane.orphaned_volumes.items() if names}
    return {**report, "orphaned_volumes": orphaned} if orphaned else report


def ship_env(
    project_root: Path, env: str, *, gh: ToolRunner | None, git: ToolRunner, check: bool
) -> dict:
    check_env_name(env)
    config = load_config(project_root)
    # The settings this commit would ship are checked before any host or cloud is touched.
    check_settings(config, read_extension_record(project_root) or {}, env)
    check_bindings(config, read_extension_record(project_root) or {})
    if check:
        found = check_facts(config, env)
        export_facts(found)
        limits = limits_per_host(config, read_extension_record(project_root) or {})
        return {**found, "service_limits": limits} if limits else found
    plane = control_plane(config, project_root, env)
    require_deployed(config, env)
    # A ship workflow runs on every push, the first deploy's own included; inside Actions it
    # finishes green with nothing shipped, so the push that records the confirmation ships.
    pending = identity_pending(config, env)
    if pending and not in_actions():
        raise Failure(ExitCode.PREREQUISITE, identity_pending_note(env))
    if not in_actions():
        if gh is None:
            raise Failure(ExitCode.INTERNAL, "dispatching the ship workflow needs the gh runner")
        warnings = require_ci_git_credential(project_root, config, env, gh)
        followed = dispatch_and_follow(project_root, SHIP_WORKFLOW, env, gh, git)
        result = {"env": env, "dispatched": True, **followed}
        return {**result, "warnings": warnings} if warnings else result
    require_bootstrapped(config, env)
    commit = head_commit(git, project_root)
    if pending:
        data = {"env": env, "dispatched": False, "commit": commit, "hosts": []}
        return {**data, "ship_skipped": identity_pending_note(env)}
    outputs = fetch_outputs(plane)
    hosts = plane.ship_hosts(project_root, config, env, outputs, commit)
    report = _services_after_ship(plane, project_root, config, outputs)
    return {"env": env, "dispatched": False, "commit": commit, "hosts": hosts, **report}


def _confirmed(env: str, confirm: str | None, prompt: Callable[[], str] | None) -> None:
    """Refuse unless the environment's name comes back, from `--confirm` or typed at `prompt`."""
    if confirm is None:
        if prompt is None:
            raise Failure(
                ExitCode.USAGE,
                f"destroy asks you to type the environment's name; with --json or without a "
                f"terminal, pass --confirm {env}",
            )
        confirm = prompt().strip()
    if confirm != env:
        raise Failure(
            ExitCode.REFUSED,
            f"the confirmation {confirm!r} does not name environment {env!r}; nothing was "
            "destroyed",
        )


def destroy_env(
    project_root: Path,
    env: str,
    *,
    confirm: str | None,
    delete_data: str | None,
    prompt: Callable[[], str] | None,
    terraform: TerraformRunner,
    notify: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> dict:
    """Snapshot every Postgres instance, lift deletion protection and destroy the environment.

    The confirmation is the environment's name, from `confirm` or typed at `prompt` (None
    when stdin is no terminal); `delete_data` must name it too, and then no snapshot is taken
    and a Snowflake root is destroyed as well. The state store and the secrets stay. Every
    refusal that needs no cloud runs before the prompt, and the prompt before any cloud call.
    """
    check_env_name(env)
    if delete_data is not None and delete_data != env:
        raise Failure(
            ExitCode.USAGE,
            f"--delete-data must name the environment being destroyed ({env}), not {delete_data!r}",
        )
    if in_actions() and confirm is None:
        raise Failure(
            ExitCode.REFUSED,
            f"destroy runs from the laptop; inside GitHub Actions it needs --confirm {env}",
        )
    config = load_config(project_root)
    plane = control_plane(config, project_root, env)
    check_tfvars(project_root, config, env)
    state_bucket = require_bootstrapped(config, env)
    _confirmed(env, confirm, prompt)
    require_terraform(terraform)
    if not in_actions():
        # A missing or expired login is refused here rather than as a Terraform backend error.
        plane.check_login()
    outputs = current_outputs(plane)
    if outputs is None and delete_data is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"environment {env!r} has no stored outputs, so destroy cannot tell which "
            f"databases to snapshot; if they hold nothing to keep, rerun with "
            f"--delete-data {env}",
        )
    done = destroy_environment(
        plane,
        env,
        terraform,
        delete_data=delete_data is not None,
        outputs=outputs,
        notify=notify,
        now=now,
    )
    # The infrastructure is gone at this point, so a failed clean-up is reported, not raised.
    left = plane.forget_outputs()
    # CI never writes vecten.toml.
    recorded_here = not in_actions()
    cleared: list[str] = []
    if recorded_here:
        from vecten.mcp_client import login_record
        from vecten.password_links import record_name
        from vecten_shared.records import remove_record

        # The run took minutes, so the file is read again and edits made meanwhile survive.
        recorded = load_config(project_root)
        recorded.environments[env].deployed_commit = None
        cleared = _clear_state_facts(recorded, env)
        save_config(project_root, recorded)
        # Both records name what the destroyed `state` database held: the session it issued
        # and the people still owed a password link.
        remove_record(project_root, login_record(env))
        remove_record(project_root, record_name(env))
    return {
        "env": env,
        "state_bucket": state_bucket,
        **done,
        "deployed_commit_cleared": recorded_here,
        "facts_cleared": cleared,
        "outputs_left": left,
    }


# Facts whose subject lives in the environment's `state` database, which destroy deletes:
# a new deploy starts with an empty one.
STATE_FACTS = ("first_user_added", "admin_link_issued", "connector_access_applied")


def _clear_state_facts(config: VectenConfig, env: str) -> list[str]:
    """Reset the facts a new deploy must walk again; the names of those that held a value.

    On a Snowflake root the MCP's identity needs the administrator's script again: the
    `PROVISIONER` role it grants either went with `--delete-data` or still owns accounts
    the new `state` does not know.
    """
    facts = config.environments[env]
    names = list(STATE_FACTS)
    if has_snowflake_root(config) and config.has("mcp"):
        names.append("provisioner_script_run")
    cleared = [name for name in names if getattr(facts, name)]
    for name in cleared:
        setattr(facts, name, [] if name == "connector_access_applied" else False)
    return cleared
