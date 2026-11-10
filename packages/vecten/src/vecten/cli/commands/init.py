from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Any

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.intake.answers import ANSWERS_FILE, read_answers, read_answers_file, source_names
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.output import ExitCode, Failure, Result
from vecten_shared.records import write_record


def collect_params(
    ctx: CliContext, target: Path, flags: dict[str, Any], answers_file: str | None
) -> ScaffoldParams:
    if answers_file:
        answers = read_answers_file(ctx.cwd / answers_file)
    else:
        answers = read_answers(target) if target.is_dir() else {}
        if not answers and target != ctx.cwd:
            answers = read_answers(ctx.cwd)
    overrides = {k: v for k, v in flags.items() if v not in (None, [], "")}
    if "has_warehouse" in overrides:
        answers["has_warehouse"] = overrides.pop("has_warehouse")
    for given in (answers, overrides):
        if isinstance(given.get("sources"), list):
            given["sources"] = source_names(given["sources"])
    return ScaffoldParams.from_answers(answers, overrides)


def _refuse_non_empty(target: Path) -> None:
    # The recorded answers file is what `init` reads in place, so it does not make the target busy.
    if not target.exists():
        return
    if not target.is_dir():
        raise Failure(ExitCode.REFUSED, f"{target} is not a directory")
    if any(p.name != ANSWERS_FILE for p in target.iterdir()):
        raise Failure(
            ExitCode.REFUSED, f"{target} is not empty; init renders into an empty directory"
        )


def register(app: typer.Typer) -> None:
    @app.command("init", help="Render a project from the intake answers.")
    def init(
        directory: str = typer.Argument("."),
        cloud: str | None = typer.Option(None, "--cloud"),
        region: str | None = typer.Option(None, "--region"),
        warehouse: str | None = typer.Option(None, "--warehouse"),
        name: str | None = typer.Option(None, "--name"),
        domain: str | None = typer.Option(None, "--domain"),
        dns_zone: str | None = typer.Option(None, "--dns-zone"),
        github_repository: str | None = typer.Option(None, "--github-repository"),
        users: str | None = typer.Option(None, "--users"),
        source: Annotated[list[str] | None, typer.Option("--source")] = None,
        size: str | None = typer.Option(None, "--size"),
        components: str | None = typer.Option(None, "--components"),
        profile: str | None = typer.Option(None, "--profile"),
        has_warehouse: str | None = typer.Option(None, "--has-warehouse"),
        warehouse_url: str | None = typer.Option(None, "--warehouse-url"),
        snowflake_account: str | None = typer.Option(
            None,
            "--snowflake-account",
            help="The ORGANIZATION-ACCOUNT the platform builds its Snowflake warehouse in.",
        ),
        answers_file: str | None = typer.Option(None, "--answers-file"),
        deploy: bool = typer.Option(
            False, "--deploy", help="Render the deploy layer into an existing project."
        ),
        json_output: bool = JsonFlag,
    ) -> None:
        flags: dict[str, Any] = {
            "cloud": cloud,
            "region": region,
            "warehouse": warehouse,
            "name": name,
            "domain": domain,
            "dns_zone": dns_zone,
            "github_repository": github_repository,
            "users": users,
            "sources": source or None,
            "size": size,
            "components": [c.strip() for c in components.split(",")] if components else None,
            "profile": profile,
            "has_warehouse": has_warehouse,
            "warehouse_url": warehouse_url,
            "snowflake_account": snowflake_account,
        }

        def run(ctx: CliContext) -> Result:
            target = (ctx.cwd / directory).resolve()
            if deploy:
                from vecten.cli.commands.init_deploy import run_init_deploy

                return run_init_deploy(ctx, target, flags, answers_file)
            # Copier renders with overwrite, so this check is the only guard on existing files.
            _refuse_non_empty(target)
            params = collect_params(ctx, target, flags, answers_file)
            created = not target.exists()
            try:
                files = render_project(target, params)
            except BaseException:
                # A half-rendered directory init made itself would make the retry refuse as busy.
                if created:
                    shutil.rmtree(target, ignore_errors=True)
                raise
            write_record(
                target, "init", {"deploy_layer": params.render_deploy, "files": len(files)}
            )
            message = f"rendered {len(files)} files into {target}"
            if params.has_warehouse:
                database = params.warehouse_locator.database
                message += f"; the MCP serves the core and marts schemas of {database}"
            if not params.render_deploy:
                message += "; deploy layer not rendered, answers incomplete"
            return Result(
                data={
                    "project_dir": str(target),
                    "components": params.components,
                    "profile": params.profile,
                    "size": params.size,
                    "deploy_layer": params.render_deploy,
                    "files": len(files),
                },
                message=message,
            )

        run_command(json_output, run)
