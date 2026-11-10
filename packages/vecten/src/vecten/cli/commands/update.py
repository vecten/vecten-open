from __future__ import annotations

import typer

from vecten.cli.runner import JsonFlag, run_command
from vecten.scaffold.update import update_project


def register(app: typer.Typer) -> None:
    @app.command("update", help="Merge the installed release into this project, preserving edits.")
    def update(json_output: bool = JsonFlag) -> None:
        run_command(json_output, lambda ctx: update_project(ctx.require_project()))
