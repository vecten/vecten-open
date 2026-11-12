from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.stack import up as up_mod
from vecten_shared.output import Result


def register(app: typer.Typer) -> None:
    @app.command("up", help="Start the local stack for the components in vecten.toml.")
    def up(json_output: bool = JsonFlag) -> None:
        def run(ctx: CliContext) -> Result:
            res = up_mod.up(ctx.require_project(), up_mod.get_runner())
            lines = [f"{name}: {url}" for name, url in res.urls.items()]
            lines += [f"{key}: {value}" for key, value in res.airflow_api.items()]
            if res.airflow_api:
                lines.append("airflow password: AIRFLOW_ADMIN_PASSWORD in .env")
            if res.left_out:
                lines.append(f"not started: {', '.join(res.left_out)}; {up_mod.SNOWFLAKE_MCP_NOTE}")
            lines += [f"service {key}: {state}" for key, state in res.services.items()]
            lines += [f"route {key}: {url}" for key, url in res.routes.items()]
            if res.services:
                lines.append(
                    f"services reserve {res.limits['memory_mb']} MB and {res.limits['cpus']:g} CPUs"
                )
            return Result(
                data={
                    "profiles": res.profiles,
                    "urls": res.urls,
                    "stack_dir": str(res.stack_dir),
                    "left_out": res.left_out,
                    "services": res.services,
                    "limits": res.limits,
                    "routes": res.routes,
                    **res.airflow_api,
                },
                message="stack is up\n" + "\n".join(lines),
            )

        run_command(json_output, run)

    @app.command("down", help="Stop the local stack; --volumes also deletes its data.")
    def down(
        volumes: bool = typer.Option(
            False, "--volumes", help="Also remove the stack's volumes, warehouse data included."
        ),
        json_output: bool = JsonFlag,
    ) -> None:
        def run(ctx: CliContext) -> Result:
            res = up_mod.down(ctx.require_project(), up_mod.get_runner(), volumes=volumes)
            msg = "stack stopped" if res.stopped else "nothing was running"
            if res.volumes_removed:
                msg += "; volumes removed"
            return Result(
                data={
                    "stopped": res.stopped,
                    "volumes_removed": res.volumes_removed,
                    "message": msg,
                },
                message=msg,
            )

        run_command(json_output, run)
