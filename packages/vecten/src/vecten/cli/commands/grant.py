import typer

from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result


def _grant(ctx, env, dataset, schema, group, revoke=False):
    from vecten.mcp_client import AdminClient
    from vecten_shared.config import load_config, save_config

    if not group or bool(dataset) == bool(schema):
        raise Failure(ExitCode.USAGE, "name one dataset or --schema, and --group")
    root = ctx.require_project()
    client = AdminClient(root, env)
    data = client.call(
        "revoke" if revoke else "grant",
        {"target": schema or dataset, "kind": "schema" if schema else "dataset", "group": group},
    )
    if data.get("reconciliation", {}).get("complete") is False:
        raise Failure(
            ExitCode.TOOL_FAILED,
            "Grant revoked; reconciliation of remaining grants is incomplete. "
            "Run vecten reconcile --json for details.",
            data,
        )
    if schema and not revoke:
        config = load_config(root)
        facts = config.environments.get(env)
        if facts:
            grants = client.call("grants")
            for connector in config.connectors:
                if schema != f"raw_{connector.name}" or not connector.access:
                    continue
                applied = {
                    g["group"] for g in grants if g["kind"] == "schema" and g["target"] == schema
                }
                if (
                    set(connector.access.groups) <= applied
                    and connector.name not in facts.connector_access_applied
                ):
                    facts.connector_access_applied.append(connector.name)
                    save_config(root, config)
    return Result(data=data, message="Grant revoked." if revoke else "Grant applied.")


def grant(
    dataset: str | None = typer.Argument(None),
    schema: str | None = typer.Option(None, "--schema"),
    group: str | None = typer.Option(None, "--group"),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    run_command(json_output, lambda ctx: _grant(ctx, env, dataset, schema, group))


def revoke(
    dataset: str | None = typer.Argument(None),
    schema: str | None = typer.Option(None, "--schema"),
    group: str | None = typer.Option(None, "--group"),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    run_command(json_output, lambda ctx: _grant(ctx, env, dataset, schema, group, True))


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
