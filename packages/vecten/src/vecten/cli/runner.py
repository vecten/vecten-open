"""The wrapper every command body runs through; command modules import from here, not app."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import typer

from vecten.cli.context import CliContext
from vecten_shared.output import ExitCode, Failure, Result, render

JsonFlag = typer.Option(False, "--json", help="Print one JSON document on stdout.")


def run_command(json_output: bool, fn: Callable[[CliContext], Result]) -> None:
    ctx = CliContext(cwd=Path.cwd(), json=json_output)
    try:
        outcome: Result | Failure = fn(ctx)
    except Failure as failure:
        outcome = failure
    except Exception as exc:  # the contract promises a code and a document, never a traceback
        outcome = Failure(ExitCode.INTERNAL, f"internal error: {type(exc).__name__}: {exc}")
    text, code = render(outcome, as_json=json_output)
    stream = sys.stdout if json_output or code == 0 else sys.stderr
    print(text, file=stream)
    raise typer.Exit(code)
