from __future__ import annotations

import typer

from vecten.cli.context import CliContext
from vecten.cli.runner import JsonFlag, run_command
from vecten.connectors.access import ACCESS_PREFIX, is_access_key, set_access
from vecten.intake.answers import ANSWERS_FILE, validate_answer, write_answer
from vecten.intake.facts import (
    ALERT_PREFIX,
    OIDC_REGISTERED_PREFIX,
    is_alert_key,
    is_mcp_key,
    is_oidc_registered_key,
    is_promotion_key,
    set_alert_fact,
    set_mcp_fact,
    set_oidc_registered_fact,
    set_promotion,
    set_wanted,
)
from vecten_shared.config import CONFIG_FILE
from vecten_shared.output import Result


def register(app: typer.Typer) -> None:
    @app.command(
        "answer",
        help=(
            "Record an intake answer in vecten-answers.yml, or a project fact in vecten.toml "
            "(access.*, mcp.*, alert_webhook.*, oidc_registered.*, deploy.promotion)"
        ),
    )
    def answer(key: str, value: str, json_output: bool = JsonFlag) -> None:
        def run(ctx: CliContext) -> Result:
            if is_access_key(key):
                root = ctx.require_project()
                connector = key[len(ACCESS_PREFIX) :]
                groups = set_access(root, connector, value)
                return Result(
                    data={"key": key, "value": groups, "file": CONFIG_FILE},
                    message=f"recorded access.groups = {groups} for {connector} in {CONFIG_FILE}",
                )
            if is_mcp_key(key):
                recorded = set_mcp_fact(ctx.require_project(), key, value)
                return Result(
                    data={"key": key, "value": recorded, "file": CONFIG_FILE},
                    message=f"recorded {key} = {recorded} in {CONFIG_FILE}",
                )
            if is_alert_key(key):
                root = ctx.require_project()
                env = key[len(ALERT_PREFIX) :]
                wanted = set_alert_fact(root, env, value)
                return Result(
                    data={"key": key, "value": wanted, "file": CONFIG_FILE},
                    message=f"recorded {key} = {'yes' if wanted else 'no'} in {CONFIG_FILE}",
                )
            if is_oidc_registered_key(key):
                root = ctx.require_project()
                set_oidc_registered_fact(root, key[len(OIDC_REGISTERED_PREFIX) :], value)
                return Result(
                    data={"key": key, "value": True, "file": CONFIG_FILE},
                    message=f"recorded {key} = yes in {CONFIG_FILE}",
                )
            if is_promotion_key(key):
                order = set_promotion(ctx.require_project(), value)
                return Result(
                    data={"key": key, "value": order, "file": CONFIG_FILE},
                    message=f"recorded {key} = {order if order else 'none'} in {CONFIG_FILE}",
                )
            project = ctx.project_root()
            if key == "sources" and project is not None:
                wanted = set_wanted(project, validate_answer(key, value))
                answers = write_answer(project, key, value)
                return Result(
                    data={
                        "key": key,
                        "value": answers[key],
                        "file": ANSWERS_FILE,
                        "wanted": wanted,
                    },
                    message=(
                        f"recorded {key} = {answers[key]} in {ANSWERS_FILE}; "
                        f"wanted = {wanted} in {CONFIG_FILE}"
                    ),
                )
            # `init --deploy` reads the answers from the project root, wherever it is run from.
            answers = write_answer(ctx.project_root() or ctx.cwd, key, value)
            return Result(
                data={"key": key, "value": answers[key], "file": ANSWERS_FILE},
                message=f"recorded {key} = {answers[key]} in {ANSWERS_FILE}",
            )

        run_command(json_output, run)
