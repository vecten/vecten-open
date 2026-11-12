import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.extensions import add_extension, secrets_message
from vecten_shared.output import Result

extension = typer.Typer(help="Install and register Python extensions.")


@extension.command("add")
def add(package: str, json_output: bool = JsonFlag) -> None:
    def run(ctx: CliContext) -> Result:
        data = add_extension(ctx.require_project(), package)
        message = f"installed {data['package']}"
        if data["unknown_groups"]:
            message += "; unknown extension groups: " + ", ".join(data["unknown_groups"])
        lines = [message, *secrets_message(data["missing_secrets"], data["undelivered"])]
        return Result(data=data, message="\n".join(lines))

    run_command(json_output, run)


@extension.command("new")
def new(
    name: str,
    template: str | None = typer.Option(
        None,
        "--template",
        help="A local directory or git+https://<host>/<repo>@<ref>[#subdirectory=<dir>].",
    ),
    no_add: bool = typer.Option(False, "--no-add", help="Copy only; do not register and lock."),
    json_output: bool = JsonFlag,
) -> None:
    """Create extensions/<name>/ from a template or the built-in skeleton, then register it."""

    def run(ctx: CliContext) -> Result:
        from vecten.extension_new import new_extension

        data = new_extension(ctx.require_project(), name, template=template, add=not no_add)
        lines = [f"created {data['path']}"]
        if data["registered"]:
            lines.append(f"registered {data['package']}")
            lines += secrets_message(data["missing_secrets"], data["undelivered"])
        else:
            lines.append(f"register it with `vecten extension add ./{data['path']}`")
        return Result(data=data, message="\n".join(lines))

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(extension, name="extension")
