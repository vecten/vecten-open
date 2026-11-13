import typer

from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result

apps = typer.Typer(help="Analytics apps that read data through the MCP.")


@apps.command(
    "connect",
    help="Create or rotate an app's principal and store its credentials; the secret is never "
    "printed.",
)
def connect(
    name: str | None = typer.Argument(None, help="The app as <extension>.<app>."),
    env: str = typer.Option("prod", "--env", help="The environment, or local for .env."),
    adopt: bool = typer.Option(
        False, "--adopt", help="Adopt an existing warehouse account after reviewing it."
    ),
    groups_only: bool = typer.Option(
        False,
        "--groups-only",
        help="Set an existing principal's groups to the binding; keep its credentials.",
    ),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.app_connect import connect_app

        if not name:
            raise Failure(ExitCode.USAGE, "app connect needs an app as <extension>.<app>")
        data = connect_app(ctx.require_project(), name, env, adopt=adopt, rotate=not groups_only)
        return Result(data=data, message=data["message"])

    run_command(json_output, run)


def register(app):
    app.add_typer(apps, name="app")
