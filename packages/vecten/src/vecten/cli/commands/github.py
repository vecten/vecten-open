from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.github import gh as gh_mod
from vecten_shared.output import Result

github = typer.Typer(help="Put the project on GitHub and keep it there.")


@github.command("init", help="Create the repository named at intake and push the project.")
def init(json_output: bool = JsonFlag) -> None:
    def run(ctx: CliContext) -> Result:
        data = gh_mod.github_init(ctx.require_project(), gh_mod.get_gh(), gh_mod.get_git())
        return Result(data=data, message=f"created and pushed {data['repository']}")

    run_command(json_output, run)


@github.command(
    "push",
    help="Commit the project's changes and push them; on a deployed project, wait for the "
    "ship the push starts.",
)
def push(
    message: str = typer.Option("vecten: project changes", "--message", "-m"),
    no_wait: bool = typer.Option(
        False, "--no-wait", help="Return once pushed, without following the ship."
    ),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        data = gh_mod.github_push(
            ctx.require_project(), gh_mod.get_gh(), gh_mod.get_git(), message, wait=not no_wait
        )
        text = f"pushed {data['commit'][:8]}"
        if data["ship"]:
            text += f"; shipped: {data['ship']['run_url']}"
        if data["promote"]:
            text += f"; run `{data['promote']}` to ship it on"
        return Result(data=data, message=text)

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(github, name="github")
