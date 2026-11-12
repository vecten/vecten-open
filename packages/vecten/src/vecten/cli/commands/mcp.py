from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten_shared.output import ExitCode, Failure, Result

mcp = typer.Typer(help="The MCP component: validate the semantic model, connect a client.")


@mcp.command("validate", help="Check every Ossie document under osi/ the way the server will.")
def validate(
    evals: bool = typer.Option(
        False, "--evals", help="Also compile every case in evals/semantic.yaml."
    ),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        # Imported here: sqlglot and jsonschema stay out of every other command's startup.
        from vecten.next.semantic import VALIDATE_RECORD, osi_digest
        from vecten_shared.config import load_config
        from vecten_shared.osi.semantic import SemanticModel
        from vecten_shared.osi.validate import validate_osi
        from vecten_shared.records import write_record

        root = ctx.require_project()
        config = load_config(root)
        osi_dir = root / "osi"
        kind = config.warehouse.kind
        report = validate_osi(osi_dir, warehouse_kind=kind)
        record = {
            "ok": report.ok,
            "errors": len(report.errors),
            "warnings": len(report.warnings),
            "osi_sha256": osi_digest(osi_dir),
        }
        lines = [f"  {issue.render()}" for issue in report.errors]
        lines += [f"  warning: {issue.render()}" for issue in report.warnings]
        if not report.ok:
            write_record(root, VALIDATE_RECORD, record)
            lines += [f"  note: {note}" for note in report.notes]
            count = len(report.errors)
            head = f"osi/: {count} error{'s' if count != 1 else ''}"
            raise Failure(ExitCode.CONFIG, "\n".join([head, *lines]), details=report.as_data())
        model = SemanticModel.load(osi_dir, kind=kind)
        counts = {
            "datasets": len(model.datasets),
            "metrics": len(model.metrics),
            "relationships": len(model.relationships),
        }
        data = {**report.as_data(), "counts": counts}
        head = f"osi/: valid ({counts['datasets']} datasets, {counts['metrics']} metrics)"
        if evals:
            # `next` reads only `ok` and the digest, so a failed eval run must record ok false.
            try:
                outcome = _run_evals(root, model, data)
            except Failure:
                write_record(root, VALIDATE_RECORD, {**record, "ok": False})
                raise
            record["evals_failed"] = outcome["failed"]
            record["ok"] = outcome["failed"] == 0
            write_record(root, VALIDATE_RECORD, record)
            failed = [r for r in outcome["results"] if not r.ok]
            lines += [f"  note: {note}" for note in data["notes"]]
            if failed:
                rendered = [f"  {r.render()}" for r in failed]
                message = f"evals/semantic.yaml: {len(failed)} of {len(outcome['results'])} failed"
                raise Failure(
                    ExitCode.CONFIG, "\n".join([head, *lines, message, *rendered]), details=data
                )
            if outcome["results"]:
                lines.append(f"  evals: {len(outcome['results'])} passed")
        else:
            write_record(root, VALIDATE_RECORD, record)
            lines += [f"  note: {note}" for note in data["notes"]]
        return Result(data=data, message="\n".join([head, *lines]))

    run_command(json_output, run)


def _run_evals(root: Path, model, data: dict) -> dict:
    """Compiles every case into `data`; a malformed file is a content error naming the case."""
    from dataclasses import asdict

    from vecten_shared.osi.evals import EVALS_PATH, load_cases, run_cases
    from vecten_shared.osi.issues import ModelError

    path = root / EVALS_PATH
    if not path.is_file():
        data["notes"].append(f"no {EVALS_PATH.as_posix()}")
        data["evals"] = {"passed": 0, "failed": 0, "cases": []}
        return {"failed": 0, "results": []}
    try:
        cases = load_cases(path)
    except ModelError as exc:
        lines = "\n".join(f"  {issue.render()}" for issue in exc.issues)
        raise Failure(ExitCode.CONFIG, f"{EVALS_PATH.as_posix()} is malformed:\n{lines}") from None
    results = run_cases(model, cases)
    failed = sum(not r.ok for r in results)
    data["evals"] = {
        "passed": len(results) - failed,
        "failed": failed,
        "cases": [asdict(r) for r in results],
    }
    data["notes"] += [f"case {r.name}: {r.skipped}" for r in results if r.skipped]
    return {"failed": failed, "results": results}


@mcp.command("import", help="Write osi/datasets.json from an existing dbt project's artifacts.")
def import_cmd(
    dbt: Annotated[Path, typer.Option("--dbt", help="The dbt project directory.")],
    schema: Annotated[str, typer.Option(help="The schema holding the mart models.")] = "marts",
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        import json

        from vecten_shared.config import load_config
        from vecten_shared.osi.generate import (
            GeneratorError,
            dataset_database,
            generate_datasets,
            render_document,
        )
        from vecten_shared.osi.validate import is_fresh
        from vecten_shared.records import write_text_atomic

        root = ctx.require_project()
        config = load_config(root)
        target = dbt.expanduser().resolve() / "target"
        artifacts = {}
        for name in ("manifest", "catalog"):
            path = target / f"{name}.json"
            if not path.is_file():
                raise Failure(
                    ExitCode.PREREQUISITE,
                    f"{path} is missing; run `dbt docs generate` with the production target "
                    f"in {dbt} first",
                )
            try:
                artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise Failure(
                    ExitCode.PREREQUISITE,
                    f"{path} is not a dbt artifact ({exc}); run `dbt docs generate` again",
                ) from None
            if not isinstance(artifacts[name].get("nodes"), dict):
                raise Failure(
                    ExitCode.PREREQUISITE,
                    f"{path} has no nodes; run `dbt docs generate` again",
                )
        schemas = sorted(
            {
                str(node.get("schema", "")).lower()
                for node in artifacts["manifest"]["nodes"].values()
                if node.get("resource_type") == "model"
            }
        )
        if schema.lower() not in schemas:
            raise Failure(
                ExitCode.CONFIG,
                f"no dbt model is in schema {schema!r}; the manifest's models are in "
                f"{', '.join(schemas) or 'no schema'}. Pass the mart schema with --schema",
            )
        try:
            doc = generate_datasets(
                artifacts["manifest"],
                artifacts["catalog"],
                database=dataset_database(config),
                model_name=config.project.name.replace("-", "_"),
                schema=schema,
            )
        except GeneratorError as exc:
            lines = "\n".join(f"  {issue.render()}" for issue in exc.issues)
            raise Failure(
                ExitCode.CONFIG, f"osi/datasets.json cannot be generated:\n{lines}"
            ) from None
        text = render_document(doc)
        osi_dir = root / "osi"
        changed = not is_fresh(osi_dir, text)
        if changed:
            osi_dir.mkdir(exist_ok=True)
            write_text_atomic(osi_dir / "datasets.json", text)
        count = len(doc["semantic_model"][0]["datasets"])
        state = "written" if changed else "unchanged"
        return Result(
            data={"datasets": count, "changed": changed, "path": "osi/datasets.json"},
            message=f"osi/datasets.json {state}: {count} datasets from {dbt}; "
            "run `vecten mcp validate` next",
        )

    run_command(json_output, run)


def connection_steps(url: str) -> list[str]:
    return [
        f"MCP server URL: {url}",
        f"Claude Code: `claude mcp add --transport http vecten {url}`",
        "Claude Desktop: Settings, Connectors, add a custom connector with the URL",
        "sign in through the browser when the client opens the login page; "
        "your admin must have added you with `vecten user add`",
    ]


@mcp.command("connection", help="Print the deployed MCP URL and how to connect a client.")
def connection(
    env: str = typer.Option("prod", "--env", help="The deployed environment to connect to."),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        from vecten.mcp_url import mcp_url
        from vecten_shared.config import load_config, save_config
        from vecten_shared.names import check_env_name

        root = ctx.require_project()
        check_env_name(env)
        config = load_config(root)
        if config.deploy is None:
            raise Failure(
                ExitCode.REFUSED,
                "the project has no deploy layer; run `vecten init --deploy` and deploy first",
            )
        if not config.has("mcp"):
            raise Failure(ExitCode.REFUSED, "the project does not include the mcp component")
        url = mcp_url(root, config, env) + "/mcp"
        steps = connection_steps(url)
        lines = [url, *(f"  {step}" for step in steps[1:])]
        facts = config.environments.get(env)
        # `next` asks for this once people can connect, which is only after the env deployed.
        recorded = facts is not None and bool(facts.deployed_commit)
        if recorded:
            facts.connection_shown = True
            save_config(root, config)
        else:
            lines.append(f"not recorded: {env} has not been deployed yet")
        return Result(
            data={"env": env, "url": url, "steps": steps, "recorded": recorded},
            message="\n".join(lines),
        )

    run_command(json_output, run)


@mcp.command("admin-link", help="Issue a one-time set-password link for a deployed MCP's admin.")
def admin_link(
    env: str = typer.Option(..., "--env", help="The deployed environment."),
    email: str | None = typer.Option(
        None, "--email", help="The admin's email; defaults to the first of [mcp] admins."
    ),
    json_output: bool = JsonFlag,
) -> None:
    def run(ctx: CliContext) -> Result:
        from vecten.mcp_admin_link import issue_admin_link

        data = issue_admin_link(ctx.require_project(), env, email)
        minutes = data["expires_in"] // 60
        return Result(
            data=data,
            message=(
                f"Open {data['url']} and enter the one-time setup token: {data['setup_token']}\n"
                f"  it works once, for {minutes} minutes, for {data['email']}"
            ),
        )

    run_command(json_output, run)


def register(app: typer.Typer) -> None:
    app.add_typer(mcp, name="mcp")


@mcp.command("provisioner-script", help="Print administrator-run warehouse setup SQL.")
def provisioner_script_cmd(
    env: str = typer.Option("local", "--env"),
    confirm_run: bool = typer.Option(
        False,
        "--confirm-run",
        help="Record that the warehouse administrator successfully ran the SQL.",
    ),
    public_key: str | None = typer.Option(
        None, "--public-key", help="Snowflake provisioner's RSA public key, without PEM headers."
    ),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_onboarding import setup_script

        data = setup_script(ctx.require_project(), env, confirm_run, public_key)
        if data["confirmed"]:
            recorded = (
                f"recorded that the administrator ran the provisioner script for {data['env']}"
            )
            message = "\n".join([recorded, *("then: " + c for c in data["commands"])])
        else:
            message = data["sql"] + "\n" + "\n".join("-- then: " + c for c in data["commands"])
        return Result(data=data, message=message)

    run_command(json_output, run)


@mcp.command("introspect", help="Draft datasets from the existing warehouse catalog.")
def introspect_cmd(
    schemas: Annotated[list[str] | None, typer.Option("--schema")] = None,
    user: str | None = typer.Option(None, "--user", help="Warehouse administrator login name."),
    replace: bool = typer.Option(False, "--replace"),
    json_output: bool = JsonFlag,
):
    def run(ctx):
        from vecten.mcp_onboarding import CONVENTION_SCHEMAS, introspect

        data = introspect(ctx.require_project(), schemas or list(CONVENTION_SCHEMAS), user, replace)
        message = (
            f"drafted {data['datasets']} datasets in osi/datasets.json; "
            "review names and add descriptions"
        )
        return Result(data=data, message="\n".join([message, *data["warnings"]]))

    run_command(json_output, run)
