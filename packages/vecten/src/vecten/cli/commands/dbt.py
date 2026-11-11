from __future__ import annotations

import sys
from pathlib import Path

import click
import typer
from typer.core import TyperCommand

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_platform.dbt.deps import write_packages_yml
from vecten_platform.dbt.profile import profile_env
from vecten_platform.dbt.runner import base_env, run_dbt
from vecten_shared.config import VectenConfig, load_config
from vecten_shared.output import ExitCode, Failure, Result, render

PASS_THROUGH = {"allow_extra_args": True, "ignore_unknown_options": True}
RAW_ARGS = "vecten.dbt.raw_args"
SNOWFLAKE_UNREACHABLE = (
    "vecten dbt without --env runs against the laptop stack, which has no Snowflake: run "
    "`vecten dbt --env <env> <args>` with a deployed environment's credentials, or "
    "`vecten dbt-build-local` for the credential-free build on the Snowflake emulation"
)


class PassThroughCommand(TyperCommand):
    """Keeps the argument list exactly as typed; click's parser drops every bare `--`."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.meta[RAW_ARGS] = list(args)
        return super().parse_args(ctx, args)


def _platform_project(ctx: CliContext) -> tuple[Path, VectenConfig]:
    root = ctx.require_project()
    config = load_config(root)
    if not config.has("platform"):
        raise Failure(
            ExitCode.REFUSED, "this project has no platform component, so it has no dbt project"
        )
    return root, config


def _dbt_env(root: Path, config: VectenConfig) -> dict[str, str]:
    from vecten_shared.provider.local import LocalProvider

    # The laptop provider's only warehouse is the stack's Postgres, which a Snowflake profile
    # cannot use.
    if config.warehouse.kind == "snowflake":
        raise Failure(ExitCode.PREREQUISITE, SNOWFLAKE_UNREACHABLE)
    provider = LocalProvider(root, inside_compose=False)
    spec = provider.connection("local", "warehouse")
    return base_env(root / "dbt", profile_env(spec, provider.get_secret("LOADER_PASSWORD")))


def register(app: typer.Typer) -> None:
    @app.command(
        "dbt",
        cls=PassThroughCommand,
        context_settings=PASS_THROUGH,
        add_help_option=False,
        help=(
            "Run dbt against the project's profile; arguments, output and exit code are dbt's own. "
            "A leading --env <env> runs it with a deployed Snowflake environment's credentials, "
            "and on prod only commands that build nothing."
        ),
    )
    def dbt(ctx: typer.Context) -> None:
        # Imported here: the environment path reaches the deploy and secrets modules.
        from vecten.dbt_env import environment_dbt_env, split_env

        cli = CliContext(cwd=Path.cwd(), json=False)
        try:
            root, config = _platform_project(cli)
            target_env, args = split_env(ctx.meta[RAW_ARGS])
            if target_env is None:
                env = _dbt_env(root, config)
                write_packages_yml(root / "dbt", config)
            else:
                env = environment_dbt_env(root, config, target_env, args)
        except Failure as failure:
            text, code = render(failure, as_json=False)
            print(text, file=sys.stderr)
            raise typer.Exit(code) from None
        run = run_dbt(args, project_dir=root / "dbt", env=env, capture=False)
        raise typer.Exit(run.returncode)

    @app.command(
        "dbt-build-local",
        help=(
            "Build and test the dbt project on a disposable warehouse with no credentials, "
            "then regenerate osi/datasets.json."
        ),
    )
    def dbt_build_local(
        check: bool = typer.Option(
            False,
            "--check",
            help="Fail when the committed osi/datasets.json is stale; write nothing.",
        ),
        json_output: bool = JsonFlag,
    ) -> None:
        def log(line: str) -> None:
            if not json_output:
                print(line, file=sys.stderr)

        def run(ctx: CliContext) -> Result:
            # Imported here: the generator and validator pull in sqlglot and jsonschema.
            from vecten_platform.dbt.build_local import build_local

            root, config = _platform_project(ctx)
            outcome = build_local(root, config, check=check, log=log, quiet=json_output)
            return Result(data=outcome.as_data(), message=outcome.message)

        run_command(json_output, run)
