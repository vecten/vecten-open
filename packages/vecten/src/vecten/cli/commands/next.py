from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import Result


def next_command(json_output: bool = JsonFlag) -> None:
    """What is missing, in priority order; reads files, run records and git only."""

    def run(ctx: CliContext) -> Result:
        from vecten.next.assemble import next_items, next_notes

        items = next_items(ctx.cwd)
        notes = next_notes(ctx.cwd)
        lines = [f"{i.kind}: {i.title} -> {i.resolution}" for i in items] or ["nothing left to do"]
        lines += [f"note: {note}" for note in notes]
        return Result(
            data={"items": [i.to_json() for i in items], "notes": notes},
            message="\n".join(lines),
        )

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.command("next")(next_command)
