import typer

from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result


def _grant(ctx, env, dataset, schema, area, group, revoke=False):
    from vecten.mcp_client import AdminClient

    named = [
        (kind, target)
        for kind, target in (("dataset", dataset), ("schema", schema), ("area", area))
        if target
    ]
    if not group or len(named) != 1:
        raise Failure(ExitCode.USAGE, "name one dataset, --schema or --area, and --group")
    [(kind, target)] = named
    root = ctx.require_project()
    client = AdminClient(root, env)
    data = client.call(
        "revoke" if revoke else "grant", {"target": target, "kind": kind, "group": group}
    )
    reconciliation = data.get("reconciliation", {})
    if reconciliation.get("complete") is False:
        refused = [
            f["error"]
            for f in reconciliation.get("failures", [])
            if (f.get("group"), f.get("kind"), f.get("target"), f.get("operation"))
            == (group, kind, target, "revoke")
        ]
        message = (
            "The revoke is recorded, but the warehouse refused it: "
            + "; ".join(refused)
            + ". Every reconciliation retries it."
            if refused
            else "Grant revoked; reconciliation of remaining grants is incomplete."
        )
        raise Failure(
            ExitCode.TOOL_FAILED, message + " Run vecten reconcile --json for details.", data
        )
    if kind != "dataset" and not revoke:
        _record_declared_access(root, env, client, kind, target)
    if data.get("refused"):
        raise Failure(
            ExitCode.TOOL_FAILED,
            "Grant applied except where the warehouse refused it: " + "; ".join(data["refused"]),
            data,
        )
    return Result(data=data, message="Grant revoked." if revoke else "Grant applied.")


def _record_declared_access(root, env, client, kind, target):
    """Once every group `vecten.toml` declares for a raw schema or an area holds its grant,
    `next` stops asking for it; a later revoke in the panel stands."""
    from vecten_shared.config import load_config, save_config

    config = load_config(root)
    facts = config.environments.get(env)
    if not facts:
        return
    if kind == "schema":
        declared = {
            connector.name: connector.access.groups
            for connector in config.connectors
            if target == f"raw_{connector.name}" and connector.access
        }
        applied = facts.connector_access_applied
    else:
        area = config.areas.get(target)
        declared = {target: area.access.groups} if area and area.access else {}
        applied = facts.area_access_applied
    if not declared:
        return
    held = {g["group"] for g in client.call("grants") if (g["kind"], g["target"]) == (kind, target)}
    for name, groups in declared.items():
        if set(groups) <= held and name not in applied:
            applied.append(name)
            save_config(root, config)


def grant(
    dataset: str | None = typer.Argument(None),
    schema: str | None = typer.Option(None, "--schema"),
    area: str | None = typer.Option(
        None, "--area", help="A restricted area: both core_<area> and marts_<area>."
    ),
    group: str | None = typer.Option(None, "--group"),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    run_command(json_output, lambda ctx: _grant(ctx, env, dataset, schema, area, group))


def revoke(
    dataset: str | None = typer.Argument(None),
    schema: str | None = typer.Option(None, "--schema"),
    area: str | None = typer.Option(
        None, "--area", help="A restricted area: both core_<area> and marts_<area>."
    ),
    group: str | None = typer.Option(None, "--group"),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    run_command(json_output, lambda ctx: _grant(ctx, env, dataset, schema, area, group, True))


def reconcile(env: str = typer.Option("prod", "--env"), json_output: bool = JsonFlag):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        data = AdminClient(ctx.require_project(), env).call("reconcile", {})
        if data.get("complete") is False:
            raise Failure(ExitCode.TOOL_FAILED, "Grant reconciliation is incomplete.", data)
        return Result(data=data, message="Grants reconciled.")

    run_command(json_output, run)


def deny(
    group: str | None = typer.Option(None, "--group"),
    columns: str = typer.Option(
        "", "--columns", help="Comma-separated dataset.field names; empty clears the list."
    ),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        if not group:
            raise Failure(ExitCode.USAGE, "deny needs --group")
        data = AdminClient(ctx.require_project(), env).call(
            "denied/set",
            {"group": group, "columns": [c.strip() for c in columns.split(",") if c.strip()]},
        )
        return Result(data=data, message="Column visibility updated.")

    run_command(json_output, run)


def register(app):
    app.command("grant")(grant)
    app.command("revoke")(revoke)
    app.command("reconcile")(reconcile)
    app.command("deny")(deny)
