from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import Result
from vecten_shared.versions import VERSIONS


def run_version(ctx: CliContext) -> Result:
    return Result(
        data={
            "vecten": VERSIONS.vecten,
            "airflow": VERSIONS.airflow_version,
            "postgres_major": VERSIONS.postgres_major,
            "ossie": VERSIONS.ossie_spec,
        },
        message=f"vecten {VERSIONS.vecten}",
    )


def register(app: typer.Typer) -> None:
    @app.command("version", help="Print the vecten version and the pinned stack versions.")
    def version(json_output: bool = JsonFlag) -> None:
        run_command(json_output, run_version)
