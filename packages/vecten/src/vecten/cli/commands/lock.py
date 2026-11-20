from typing import Annotated

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result


def lock(
    upgrade: Annotated[
        list[str] | None,
        typer.Option("--upgrade", help="Let this package move in every lock; repeatable."),
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Compare local extension declarations without writing or resolving locks.",
        ),
    ] = False,
    json_output: bool = JsonFlag,
) -> None:
    """Resolve requirements.txt, the per-component locks and extensions.lock.json together."""

    def run(ctx: CliContext) -> Result:
        from vecten.extensions import check_declarations, secrets_message
        from vecten.extensions import lock as lock_project

        root = ctx.require_project()
        if check:
            if upgrade:
                raise Failure(ExitCode.USAGE, "--check and --upgrade cannot be combined")
            names = check_declarations(root)
            return Result(
                data={"checked": names}, message=f"checked {len(names)} in-repo extensions"
            )
        data = lock_project(root, tuple(upgrade or ()))
        files = ["requirements.txt", *data["locks"], "extensions.lock.json"]
        lines = ["wrote " + ", ".join(files)]
        if data["divergence"]:
            lines.append("pinned differently across locks: " + ", ".join(data["divergence"]))
        lines += secrets_message(data["missing_locally"], data["undelivered"])
        if data["unsupported"]:
            lines.append(
                "manifest and service keys this release does not support: "
                + "; ".join(f"{n}: {', '.join(k)}" for n, k in data["unsupported"].items())
            )
        return Result(data=data, message="\n".join(lines))

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.command("lock")(lock)
