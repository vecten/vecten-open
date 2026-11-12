from __future__ import annotations

import sys

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.github import gh as gh_mod
from vecten_shared.output import ExitCode, Failure, Result


def _facts_message(data: dict) -> str:
    facts = " ".join(
        f"{name}={str(value).lower()}" for name, value in data.items() if name != "service_limits"
    )
    lines = [
        f"services on {host} reserve {limit['memory_mb']} MB and {limit['cpus']:g} CPUs"
        for host, limit in data.get("service_limits", {}).items()
    ]
    return "\n".join([facts, *lines])


def _ship_message(data: dict) -> str:
    if data["dispatched"]:
        shipped = f"run {data['run_id']} shipped {data['head_sha'][:8]}: {data['url']}"
        if data["matches_local_head"]:
            return shipped
        # The workflow checks out the default branch as GitHub holds it, not this checkout.
        return (
            f"{shipped}\nCI shipped {data['branch']} at {data['head_sha'][:8]}; "
            "local commits were not included (push them with `vecten github push`)"
        )
    if data.get("ship_skipped"):
        return f"shipped nothing: {data['ship_skipped']}"
    return "\n".join(
        [f"shipped {data['commit'][:8]} to {', '.join(data['hosts'])}", *_service_lines(data)]
    )


def _service_lines(data: dict) -> list[str]:
    lines = [f"service {key}: {state}" for key, state in data.get("services", {}).items()]
    if data.get("waiting_routes"):
        lines.append(
            f"routes waiting for `vecten deploy --env {data['env']}`: "
            + ", ".join(data["waiting_routes"])
        )
    lines += [
        f"volumes of services no longer bound on {host}: {', '.join(names)}; they keep their "
        "data until removed on the VM"
        for host, names in data.get("orphaned_volumes", {}).items()
    ]
    return lines


def ship(
    env: str | None = typer.Option(None, "--env", help="The environment to ship to."),
    check: bool = typer.Option(
        False,
        "--check",
        help="Report whether the environment is bootstrapped and deployed; without --env, "
        "for the first environment of the promotion order.",
    ),
    commit: str | None = typer.Option(
        None,
        "--commit",
        help="Inside GitHub Actions, the default branch's commit to ship instead of the "
        "checked-out one.",
    ),
    json_output: bool = JsonFlag,
) -> None:
    """Send the project's content to every host of an environment; never runs Terraform."""

    def run(ctx: CliContext) -> Result:
        if not env and not check:
            raise Failure(ExitCode.USAGE, "ship needs --env <env>")
        # Deferred so that importing the CLI loads no deploy machinery or boto3.
        from vecten.deploy.run import ship_env

        root = ctx.require_project()
        data = ship_env(
            root, env or None, gh=gh_mod.get_gh(), git=gh_mod.get_git(), check=check, commit=commit
        )
        return Result(data=data, message=_facts_message(data) if check else _ship_message(data))

    run_command(json_output, run)


def _identities(data: dict) -> str:
    """The CI identities the cloud's part of bootstrap reported, by the keys it reports."""
    if "role_arn" in data:
        return f"deploy role {data['role_arn']}, plan role {data['plan_role_arn']}"
    return (
        f"deploy application {data['deploy_application']}, plan application "
        f"{data['plan_application']}, host applications {', '.join(data['host_applications'])}"
    )


def _bootstrap_message(data: dict) -> str:
    created = ", ".join(data["created"]) or "nothing (all present)"
    env_names = [*data["environment_variables"], *data.get("environment_secrets", [])]
    repo_names = [*data["repository_variables"], *data.get("repository_secrets", [])]
    message = (
        f"bootstrapped {data['env']}: state bucket {data['state_bucket']}, "
        f"{_identities(data)}; created {created}; GitHub environment {data['env']} holds "
        f"{', '.join(env_names)}"
    )
    if repo_names:
        message += f"; {data['repository']} holds {', '.join(repo_names)}"
    for kind in ("rotated", "deleted"):
        if data.get(kind):
            message += f"; {kind} {', '.join(data[kind])}"
    if data.get("awaiting_ack"):
        message += (
            f"; the old keys of {', '.join(data['awaiting_ack'])} stay until each host "
            "acknowledges its new key, and a later bootstrap deletes them; run "
            f"`vecten deploy --env {data['env']}` to hand the hosts their new keys"
        )
    if data.get("ci_keys_retiring"):
        message += (
            f"; the replaced {' and '.join(data['ci_keys_retiring'])} keys stay valid for "
            "workflow runs already under way, and the next bootstrap deletes them"
        )
    if data["environment_branch_policy"] == "default-branch":
        message += f"; deployments to {data['env']} are limited to the default branch"
    if "repository_note" in data:
        message += f"\n{data['repository_note']}"
    for warning in data.get("warnings", []):
        message += f"\nwarning: {warning}"
    return message


def bootstrap(
    env: str | None = typer.Option(None, "--env", help="The environment to prepare for CI."),
    json_output: bool = JsonFlag,
) -> None:
    """Prepare an environment for CI from the laptop with your cloud login: the Terraform
    state bucket and the identities CI deploys and plans with in the cloud (on AWS the GitHub
    OIDC provider, the host roles' permissions boundary and the deploy and plan roles; on
    Scaleway the resources and state projects and an IAM application and key for deploy,
    plan and each host); the GitHub environment, deployable from the default branch, with its
    variables and secrets, and the plan identity at repository level on GitHub."""

    def run(ctx: CliContext) -> Result:
        if not env:
            raise Failure(ExitCode.USAGE, "bootstrap needs --env <env>")
        # Deferred so that importing the CLI loads no deploy machinery or boto3.
        from vecten.deploy.run import bootstrap_env

        root = ctx.require_project()
        data = bootstrap_env(root, env, gh=gh_mod.get_gh())
        return Result(data=data, message=_bootstrap_message(data))

    run_command(json_output, run)


def _deploy_message(data: dict) -> str:
    return "\n".join(
        [
            _deployed_message(data),
            *_service_lines(data),
            *(f"warning: {w}" for w in data.get("warnings", [])),
        ]
    )


def _deployed_message(data: dict) -> str:
    if data["plan_only"]:
        return f"planned {', '.join(data['roots'])} for {data['env']}: {data['plan_file']}"
    if data["dispatched"]:
        sha = data["deployed_commit"][:8]
        message = (
            f"run {data['run_id']} deployed {sha} to {data['env']}: {data['url']}\n"
            f"vecten.toml records deployed_commit {sha}; push it with `vecten github push` "
            "so the ship workflow runs on merge"
        )
        if data.get("ship_skipped"):
            message += f"\n{data['ship_skipped']}"
        return message
    if data.get("ship_skipped"):
        return f"applied {', '.join(data['roots'])} and shipped nothing: {data['ship_skipped']}"
    return (
        f"applied {', '.join(data['roots'])} and shipped {data['commit'][:8]} to "
        f"{', '.join(data['hosts'])}"
    )


def deploy(
    env: str | None = typer.Option(None, "--env", help="The environment to deploy."),
    plan_only: bool = typer.Option(
        False,
        "--plan-only",
        help="Plan with Terraform and apply nothing; the first plan of a Snowflake project "
        "stores the loader key it generates in the secrets store.",
    ),
    check: bool = typer.Option(
        False, "--check", help="Report whether the environment is bootstrapped and deployed."
    ),
    cloud_only: bool = typer.Option(
        False,
        "--cloud-only",
        help="With --plan-only, plan the cloud root alone and read no Snowflake secret.",
    ),
    commit: str | None = typer.Option(
        None,
        "--commit",
        help="For an environment after the first in the promotion order, a commit the "
        "environment before it shipped, instead of the newest one.",
    ),
    json_output: bool = JsonFlag,
) -> None:
    """Create or update an environment's infrastructure and ship every host. Inside GitHub
    Actions it runs Terraform plan and apply; from the laptop it dispatches the apply
    workflow on the default branch, follows it and records the deployed commit."""

    def run(ctx: CliContext) -> Result:
        if not env:
            raise Failure(ExitCode.USAGE, "deploy needs --env <env>")
        # Deferred so that importing the CLI loads no deploy machinery or boto3.
        from vecten.deploy.run import deploy_env
        from vecten.deploy.terraform import get_terraform

        root = ctx.require_project()
        data = deploy_env(
            root,
            env,
            plan_only=plan_only,
            check=check,
            gh=gh_mod.get_gh(),
            git=gh_mod.get_git(),
            terraform=get_terraform(),
            cloud_only=cloud_only,
            commit=commit,
        )
        return Result(data=data, message=_facts_message(data) if check else _deploy_message(data))

    run_command(json_output, run)


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _progress(line: str) -> None:
    # stdout carries the one result document, so progress goes to stderr.
    print(line, file=sys.stderr, flush=True)


def _destroy_message(data: dict) -> str:
    # The snapshot identifiers were printed as progress before Terraform ran.
    lines = []
    if data["data_deleted"]:
        lines.append("no snapshot taken: --delete-data deleted the databases")
    else:
        lines.append(f"{len(data['snapshots'])} final snapshot(s) kept, listed above")
    if data["snapshot_expiry"]:
        # One date: the snapshots are taken together, and their names were printed already.
        first = min(data["snapshot_expiry"].values())[:10]
        lines.append(
            f"the cloud deletes these snapshots on {first} and keeps none longer; to keep the "
            "data past that date, restore a snapshot and take a new snapshot of the restored "
            "instance (runbook, Destroy)"
        )
    if data["destroyed"]:
        lines.append(f"destroyed {', '.join(data['destroyed'])} in {data['env']}")
    if data["already_destroyed"]:
        lines.append(
            f"{', '.join(data['already_destroyed'])} in {data['env']} held nothing to destroy"
        )
    lines.append(f"the state bucket {data['state_bucket']} and the secrets were kept")
    if data["snowflake"] == "kept":
        lines.append(
            f"the Snowflake database was kept; `vecten destroy --env {data['env']} --confirm "
            f"{data['env']} --delete-data {data['env']}` deletes it"
        )
    if data["snowflake"] == "kept" and "provisioner_script_run" in data["facts_cleared"]:
        lines.append(
            "the users and roles PROVISIONER created stay in Snowflake, and the MCP refuses to "
            "start beside ones its new state does not know; before the next deploy, restore "
            f"the state snapshot, or run `vecten mcp provisioner-script --env {data['env']}`, "
            "whose last query prints a DROP for each"
        )
    if data["facts_cleared"]:
        lines.append(
            "a new deploy starts with an empty state database, so vecten.toml no longer "
            "records the MCP setup steps kept there; `vecten next` walks them again"
        )
    for left in data["outputs_left"]:
        lines.append(f"warning: the infrastructure is destroyed, but {left} was not removed")
    if data["deployed_commit_cleared"]:
        lines.append(
            "vecten.toml no longer records a deployed commit; push it with `vecten github "
            "push` so the ship workflow stops shipping"
        )
    return "\n".join(lines)


def destroy(
    env: str | None = typer.Option(None, "--env", help="The environment to destroy."),
    confirm: str | None = typer.Option(
        None,
        "--confirm",
        help="The environment's name again, confirming without a prompt (for CI teardown).",
    ),
    delete_data: str | None = typer.Option(
        None,
        "--delete-data",
        help="The environment's name again: take no final snapshot of the databases and also "
        "destroy the Snowflake database.",
    ),
    json_output: bool = JsonFlag,
) -> None:
    """Destroy an environment's infrastructure from the laptop with your cloud login, after a
    final snapshot of every Postgres instance. Asks you to type the environment's name; the
    Terraform state bucket and the secrets stay."""

    def run(ctx: CliContext) -> Result:
        if not env:
            raise Failure(ExitCode.USAGE, "destroy needs --env <env>")
        # Deferred so that importing the CLI loads no deploy machinery or boto3.
        from vecten.deploy.run import destroy_env
        from vecten.deploy.terraform import get_terraform

        root = ctx.require_project()

        def typed_back() -> str:
            return typer.prompt(
                f"type {env} to destroy it", default="", show_default=False, err=True
            )

        data = destroy_env(
            root,
            env,
            confirm=confirm,
            delete_data=delete_data,
            # The prompt would interleave with the one JSON document, so --json never asks.
            prompt=typed_back if _stdin_is_tty() and not json_output else None,
            terraform=get_terraform(),
            notify=_progress,
        )
        return Result(data=data, message=_destroy_message(data))

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.command("bootstrap")(bootstrap)
    app.command("deploy")(deploy)
    app.command("ship")(ship)
    app.command("destroy")(destroy)
