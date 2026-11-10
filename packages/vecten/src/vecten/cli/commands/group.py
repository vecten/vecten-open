import typer

from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result

group = typer.Typer(help="MCP groups and membership.")


@group.command("add")
def add(
    name: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        if not name:
            raise Failure(ExitCode.USAGE, "group add needs a name")
        client = AdminClient(ctx.require_project(), env)
        existing = next((g for g in client.call("groups") if g["name"] == name), None)
        data = existing or client.call("groups/create", {"name": name})
        return Result(data=data, message=f"Group {name} is available.")

    run_command(json_output, run)


@group.command("list")
def list_groups(env: str = typer.Option("prod", "--env"), json_output: bool = JsonFlag):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        data = AdminClient(ctx.require_project(), env).call("groups")
        return Result(data={"groups": data}, message=str(data))

    run_command(json_output, run)


@group.command("delete")
def delete(
    name: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        if not name:
            raise Failure(ExitCode.USAGE, "group delete needs a name")
        data = AdminClient(ctx.require_project(), env).call("groups/delete", {"name": name})
        return Result(data=data, message=f"Deleted group {name}.")

    run_command(json_output, run)


def member(group_name, principal_id, enabled, env, json_output):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        if not group_name or not principal_id:
            raise Failure(ExitCode.USAGE, "membership needs a group and principal id")
        data = AdminClient(ctx.require_project(), env).call(
            "groups/member", {"group": group_name, "id": principal_id, "member": enabled}
        )
        return Result(data=data, message="Membership updated.")

    run_command(json_output, run)


@group.command("add-member")
def add_member(
    name: str | None = typer.Argument(None),
    principal_id: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    member(name, principal_id, True, env, json_output)


@group.command("remove-member")
def remove_member(
    name: str | None = typer.Argument(None),
    principal_id: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    member(name, principal_id, False, env, json_output)


def register(app):
    app.add_typer(group, name="group")
