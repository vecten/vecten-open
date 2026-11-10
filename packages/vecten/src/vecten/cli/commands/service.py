import typer

from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result

service = typer.Typer(help="Independent MCP service principals.")


@service.command("add")
def add(
    name: str | None = typer.Argument(None),
    adopt: bool = typer.Option(False, "--adopt"),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        if not name:
            raise Failure(ExitCode.USAGE, "service add needs a name")
        data = AdminClient(ctx.require_project(), env).call(
            "services/create", {"name": name, "adopt": adopt}
        )
        credentials = data["credentials"]
        return Result(
            data=data,
            message=f"Created {data['warehouse_name']}. Save these credentials now: "
            f"client id {credentials['client_id']}, secret {credentials['client_secret']}",
        )

    run_command(json_output, run)


@service.command("list")
def list_services(env: str = typer.Option("prod", "--env"), json_output: bool = JsonFlag):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        data = AdminClient(ctx.require_project(), env).call("services")
        return Result(data={"services": data}, message=str(data))

    run_command(json_output, run)


def action(operation, principal_id, env, json_output):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        if not principal_id:
            raise Failure(ExitCode.USAGE, "service operation needs a principal id")
        data = AdminClient(ctx.require_project(), env).call(
            "services/" + operation, {"id": principal_id}
        )
        return Result(data=data, message=f"{operation}: {principal_id}")

    run_command(json_output, run)


@service.command("disable")
def disable(
    principal_id: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    action("disable", principal_id, env, json_output)


@service.command("delete")
def delete(
    principal_id: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    action("delete", principal_id, env, json_output)


def register(app):
    app.add_typer(service, name="service")
