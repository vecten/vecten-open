from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.connectors.names import check_connector_name
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure, Result

# The connector modules import inside the command bodies: `connector run` pulls in dlt, which
# every other command would otherwise pay for at startup.
connector = typer.Typer(help="Wire dlt sources into the project and run them locally.")


def _relock(root, name: str, dependencies: list[str]) -> bool:
    """Re-lock a project that has locks, so the new dependencies are pinned like the rest.

    A connector without dependencies leaves fresh locks fresh, and re-locking needs Docker.
    """
    from vecten import extensions as project_extensions
    from vecten import locking

    if not any((root / f).is_file() for f in locking.LOCK_FILES.values()):
        return False
    if not dependencies and not locking.lock_problems(root, load_config(root)):
        return True
    try:
        project_extensions.lock(root)
    except Failure as failure:
        raise Failure(
            failure.code,
            f"added {name}, but its dependencies are not locked yet ({failure.message}); "
            "fix that and run `vecten lock`",
        ) from None
    return True


@connector.command(
    "add", help="Wire a dlt verified source, an extension connector or a generic scaffold."
)
def add(
    name: str | None = typer.Argument(None, help="The dlt verified source or connector name."),
    from_kind: str | None = typer.Option(
        None, "--from", help="Generic source: rest, sql or filesystem."
    ),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        from vecten.connectors.add import add_connector
        from vecten.connectors.extensions import extension_connectors
        from vecten.connectors.verified import get_verified_sources

        if not name:
            raise Failure(ExitCode.USAGE, "connector add needs a source name")
        check_connector_name(name)
        root = ctx.require_project()
        extensions = extension_connectors(load_config(root)) if from_kind is None else {}
        res = add_connector(root, name, from_kind, get_verified_sources(), extensions)
        locked = _relock(root, name, res.dependencies)
        lines = [
            f"added {name} ({res.source}); files: {len(res.files)}; dags: {', '.join(res.dags)}"
        ]
        if res.secrets:
            lines.append(
                "secrets to set: " + ", ".join(f"`vecten secret set {s}`" for s in res.secrets)
            )
        if res.dependencies and locked:
            lines.append("python requirements locked: " + ", ".join(res.dependencies))
        elif res.dependencies:
            lines.append(
                "python requirements (installed in the Airflow image; install them where vecten "
                "runs, for example `uv tool install vecten --with <package>`): "
                + ", ".join(res.dependencies)
            )
        return Result(
            data={
                "name": res.name,
                "source": res.source,
                "files": res.files,
                "dags": res.dags,
                "secrets": res.secrets,
                "dependencies": res.dependencies,
                "locked": locked,
            },
            message="\n".join(lines),
        )

    run_command(json_output, run)


@connector.command("run", help="Run a source's sync locally, outside Airflow.")
def run_cmd(
    name: str | None = typer.Argument(None, help="The connector to sync."),
    object_name: str | None = typer.Option(None, "--object", help="Sync only this object."),
    record: bool = typer.Option(
        False, "--record", help="Save the extracted items as fixtures for the rendered test."
    ),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        from vecten.connectors.run import run_connector

        if not name:
            raise Failure(ExitCode.USAGE, "connector run needs a source name")
        res = run_connector(ctx.require_project(), name, object_name, record)
        if not res.ok:
            failed = "; ".join(f"{o.object} ({o.error})" for o in res.objects if not o.ok)
            # The per-object outcomes ride in details so --json shows them beside the error.
            raise Failure(
                ExitCode.TOOL_FAILED, f"{name}: objects failed: {failed}", details=res.as_data()
            )
        lines = [f"{o.object}: {o.rows} rows" for o in res.objects]
        if res.recorded:
            lines.append(f"fixtures recorded under connectors/{name}/fixtures/")
        return Result(data=res.as_data(), message="\n".join(lines))

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(connector, name="connector")
