from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import Result


def next_command(json_output: bool = JsonFlag) -> None:
    """What is missing, in priority order; reads files, run records and git only."""

    def run(ctx: CliContext) -> Result:
        from vecten.next.assemble import next_items

        items = next_items(ctx.cwd)
        lines = [f"{i.kind}: {i.title} -> {i.resolution}" for i in items]
        return Result(
            data={"items": [i.to_json() for i in items]},
            message="\n".join(lines) or "nothing left to do",
        )

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.command("next")(next_command)
