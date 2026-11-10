import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.extensions import add_extension
from vecten_shared.output import Result

extension = typer.Typer(help="Install and register Python extensions.")


@extension.command("add")
def add(package: str, json_output: bool = JsonFlag) -> None:
    def run(ctx: CliContext) -> Result:
        data = add_extension(ctx.require_project(), package)
        message = f"installed {data['package']}"
        if data["unknown_groups"]:
            message += "; unknown extension groups: " + ", ".join(data["unknown_groups"])
        return Result(data=data, message=message)

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(extension, name="extension")
