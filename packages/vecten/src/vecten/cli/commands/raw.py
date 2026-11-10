from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result

raw = typer.Typer(help="Inspect the raw schemas syncs have landed.")


@raw.command("schema", help="Dump the tables and columns of raw_<source>.")
def schema(
    name: str | None = typer.Argument(None, help="The connector whose raw schema to dump."),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        # Imported here: it pulls in dlt, which no other command should load at startup.
        from vecten.connectors.raw import raw_schema

        if not name:
            raise Failure(ExitCode.USAGE, "raw schema needs a source name")
        data = raw_schema(ctx.require_project(), name)
        if not data["tables"]:
            message = f"{data['schema']}: no tables yet; run `vecten connector run {name}`"
        else:
            lines = [f"{data['schema']}:"]
            for table in data["tables"]:
                columns = ", ".join(f"{c['name']} {c['type']}" for c in table["columns"])
                lines.append(f"  {table['name']}: {columns}")
            message = "\n".join(lines)
        return Result(data=data, message=message)

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(raw, name="raw")
