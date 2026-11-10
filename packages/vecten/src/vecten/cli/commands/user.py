from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result

user = typer.Typer(help="Warehouse accounts for people.")


@user.command(
    "add-admin",
    help="Create the warehouse login admin_<firstname> with full rights, from the laptop over "
    "the instance's master credential through an SSM tunnel; its password goes to the "
    "secrets store as ADMIN_<FIRSTNAME>_PASSWORD and only the secret's id is printed.",
)
def add_admin_cmd(
    firstname: str | None = typer.Argument(None, help="The admin's first name, for example jane."),
    env: str | None = typer.Option(None, "--env", help="The deployed environment."),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        if not firstname:
            raise Failure(ExitCode.USAGE, "user add-admin needs a first name")
        if not env:
            raise Failure(ExitCode.USAGE, "user add-admin needs --env <env>")
        # Deferred so that importing the CLI loads no boto3 or Postgres driver.
        from vecten.deploy.admin import add_admin

        data = add_admin(ctx.require_project(), firstname, env)
        if data["created"]:
            message = f"created {data['role']} in {data['env']}; its password is in "
        elif data["password_rotated"]:
            message = (
                f"{data['role']} already existed in {data['env']} without a stored password, so "
                "it got a new one, in "
            )
        else:
            message = (
                f"kept the existing {data['role']} in {data['env']}; its password was not "
                "changed and stays in "
            )
        return Result(data=data, message=message + data["secret_id"])

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(user, name="user")


@user.command("add")
def add_user(
    email: str | None = typer.Option(None, "--email"),
    name: str | None = typer.Option(None, "--name"),
    warehouse_name: str | None = typer.Option(None, "--warehouse-name"),
    adopt: bool = typer.Option(False, "--adopt"),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_client import AdminClient
        from vecten.password_links import added, read_links
        from vecten_shared.config import EnvironmentFacts, load_config, save_config
        from vecten_shared.records import write_record

        if not email or not name:
            raise Failure(ExitCode.USAGE, "user add needs --email and --name")
        root = ctx.require_project()
        owes_link = load_config(root).mcp.auth == "password"
        if owes_link:
            # A corrupt record stops the add before the MCP creates a person `next` could not
            # remember.
            read_links(root, env)
        data = AdminClient(root, env).call(
            "users/create",
            {"email": email, "name": name, "adopt": adopt, "warehouse_name": warehouse_name},
        )
        if owes_link:
            added(root, env, data["id"], email)
        config = load_config(root)
        if env != "local":
            if env not in config.environments:
                config.environments[env] = EnvironmentFacts()
            config.environments[env].first_user_added = True
            save_config(root, config)
        write_record(root, f"users/{env}", {"first_user_added": True})
        return Result(data=data, message=f"Added {email}.")

    run_command(json_output, run)


@user.command("list")
def list_users(env: str = typer.Option("prod", "--env"), json_output: bool = JsonFlag):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        data = AdminClient(ctx.require_project(), env).call("users")
        return Result(data={"users": data}, message=str(data))

    run_command(json_output, run)


def _principal_action(action, principal_id, env, json_output, extra=None):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        if not principal_id:
            raise Failure(ExitCode.USAGE, f"user {action} needs a principal id")
        root = ctx.require_project()
        if action in ("disable", "delete"):
            from vecten.password_links import dropped, read_links

            # A corrupt record fails before the MCP changes the person.
            read_links(root, env)
        data = AdminClient(root, env).call("users/" + action, {"id": principal_id, **(extra or {})})
        if action in ("disable", "delete"):
            dropped(root, env, principal_id)
        return Result(data=data, message=f"{action}: {principal_id}")

    run_command(json_output, run)


@user.command("disable")
def disable_user(
    principal_id: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    _principal_action("disable", principal_id, env, json_output)


@user.command("delete")
def delete_user(
    principal_id: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    _principal_action("delete", principal_id, env, json_output)


@user.command("set-admin")
def set_admin(
    principal_id: str | None = typer.Argument(None),
    clear: bool = typer.Option(False, "--clear"),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    _principal_action("set-admin", principal_id, env, json_output, {"admin": not clear})


@user.command("password-link")
def password_link(
    principal_id: str | None = typer.Argument(None),
    env: str = typer.Option("prod", "--env"),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_client import AdminClient

        if not principal_id:
            raise Failure(ExitCode.USAGE, "user password-link needs a principal id")
        from vecten.password_links import issued, read_links

        root = ctx.require_project()
        # A corrupt record fails before the MCP issues a token that would go unrecorded.
        read_links(root, env)
        data = AdminClient(root, env).call("users/password-link", {"id": principal_id})
        issued(root, env, principal_id)
        return Result(
            data=data,
            message=f"Open {data['url']} and use the one-time setup token: {data['setup_token']}",
        )

    run_command(json_output, run)
