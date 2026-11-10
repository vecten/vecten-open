from __future__ import annotations

import sys

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.secrets import (
    check_request,
    cloud_store,
    list_secret_names,
    read_value,
    set_secret,
)
from vecten_shared.output import ExitCode, Failure, Result

secret = typer.Typer(
    help="Store and list secrets through the provider; values never go on the command line."
)


@secret.command("set", help="Store a secret from the prompt or standard input.")
def set_cmd(
    name: str | None = typer.Argument(None, help="The secret name, for example HUBSPOT_API_KEY."),
    value_argument: str | None = typer.Argument(None, hidden=True),
    env: str | None = typer.Option(
        None, "--env", help="Write the environment's cloud secrets store."
    ),
    from_local: bool = typer.Option(
        False, "--from-local", help="Copy the value from .env without showing it."
    ),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        # The value never appears in this message: argv lands in shell history and process lists.
        if value_argument is not None:
            raise Failure(
                ExitCode.USAGE,
                "the value must come from the prompt or standard input, never a command argument",
            )
        if not name:
            raise Failure(ExitCode.USAGE, "secret set needs a secret name")
        root = ctx.require_project()
        check_request(name, env, from_local)
        if env is not None:
            # A missing deploy layer or cloud refuses before the user types a secret.
            cloud_store(root, env)
        value = None
        if not from_local:
            stdin_text = None if sys.stdin.isatty() else sys.stdin.read()
            value = read_value(
                stdin_text, lambda: typer.prompt(f"value for {name}", hide_input=True)
            )
        data = set_secret(root, name, value, env, from_local)
        where = ".env" if data["env"] == "local" else f"the {data['env']} secrets store"
        return Result(data=data, message=f"stored {name} in {where}")

    run_command(json_output, run)


@secret.command("list", help="List secret names; values are never printed.")
def list_cmd(
    env: str | None = typer.Option(None, "--env", help="List the environment's cloud store."),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        names = list_secret_names(ctx.require_project(), env)
        return Result(data={"env": env or "local", "names": names}, message="\n".join(names))

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(secret, name="secret")
