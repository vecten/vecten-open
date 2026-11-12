from typing import Annotated

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import Result


def lock(
    upgrade: Annotated[
        list[str] | None,
        typer.Option("--upgrade", help="Let this package move in every lock; repeatable."),
    ] = None,
    json_output: bool = JsonFlag,
) -> None:
    """Resolve requirements.txt, the per-component locks and extensions.lock.json together."""

    def run(ctx: CliContext) -> Result:
        from vecten.extensions import lock as lock_project

        data = lock_project(ctx.require_project(), tuple(upgrade or ()))
        message = "wrote " + ", ".join(["requirements.txt", *data["locks"]])
        if data["divergence"]:
            message += "; pinned differently across locks: " + ", ".join(data["divergence"])
        return Result(data=data, message=message)

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.command("lock")(lock)
