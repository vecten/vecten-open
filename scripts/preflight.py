#!/usr/bin/env python3
"""Run CI's lint and unit jobs as Makefile targets, one readable line per step and a summary."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, TextIO

TAIL_LINES = 30
STEP_TIMEOUT_SECONDS = 30 * 60
TICK_SECONDS = 1.0
POLL_SECONDS = 0.2
TIMED_OUT_EXIT = 124
INTERRUPTED_EXIT = 130
GREEN, RED, DIM, RESET, CLEAR_LINE = "\x1b[32m", "\x1b[31m", "\x1b[2m", "\x1b[0m", "\x1b[K"


class StepOutcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Step:
    target: str
    title: str


# Sync comes first: every later step runs in the environment it installs.
STEPS = (
    Step("sync", "Sync the lock"),
    Step("lint", "Ruff lint and format"),
    Step("comments", "Comment lint on this branch"),
    Step("wheels", "Release wheel contracts"),
    Step("versions", "Versions table"),
    Step("test", "Unit tests"),
)


@dataclass(frozen=True, slots=True)
class StepRun:
    exit_code: int
    seconds: float
    tail: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StepResult:
    step: Step
    outcome: StepOutcome
    seconds: float
    log: Path
    tail: tuple[str, ...]


class StepRunner(Protocol):
    def __call__(self, step: Step, log: Path, tick: Callable[[float], None]) -> StepRun: ...


class Reporter(Protocol):
    def begin(self, steps: Sequence[Step], log_dir: Path) -> None: ...
    def started(self, index: int, step: Step) -> None: ...
    def ticked(self, elapsed: float) -> None: ...
    def finished(self, index: int, result: StepResult) -> None: ...
    def summarise(self, results: Sequence[StepResult]) -> None: ...


def format_duration(seconds: float) -> str:
    if round(seconds, 1) < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def exit_code(results: Sequence[StepResult]) -> int:
    return 0 if all(r.outcome is StepOutcome.PASSED for r in results) else 1


def run_preflight(
    steps: Sequence[Step], runner: StepRunner, log_dir: Path, reporter: Reporter
) -> list[StepResult]:
    """Run every step in order; a failed sync skips the rest, any other failure does not."""
    reporter.begin(steps, log_dir)
    results: list[StepResult] = []
    for index, step in enumerate(steps, start=1):
        log = log_dir / f"{step.target}.log"
        if results and results[0].outcome is not StepOutcome.PASSED:
            result = StepResult(step, StepOutcome.SKIPPED, 0.0, log, ())
        else:
            reporter.started(index, step)
            run = runner(step, log, reporter.ticked)
            outcome = StepOutcome.PASSED if run.exit_code == 0 else StepOutcome.FAILED
            result = StepResult(step, outcome, run.seconds, log, run.tail)
        results.append(result)
        reporter.finished(index, result)
    reporter.summarise(results)
    return results


class TerminalReporter:
    """One aligned line per step; on a live terminal the running line shows its elapsed time."""

    def __init__(self, stream: TextIO, *, color: bool, live: bool) -> None:
        self._stream = stream
        self._color = color
        self._live = live
        self._total = 0
        self._width = 0
        self._running = ""

    def begin(self, steps: Sequence[Step], log_dir: Path) -> None:
        self._total = len(steps)
        self._width = max(len(self._label(i, s)) for i, s in enumerate(steps, start=1)) + 4
        self._write(f"Preflight: CI's lint and unit jobs (logs in {log_dir})\n\n")

    def started(self, index: int, step: Step) -> None:
        self._running = self._prefix(index, step)
        if self._live:
            self._write(f"\r{self._running}")

    def ticked(self, elapsed: float) -> None:
        if self._live:
            elapsed_text = self._paint(DIM, format_duration(elapsed))
            self._write(f"\r{self._running} {elapsed_text}{CLEAR_LINE}")

    def finished(self, index: int, result: StepResult) -> None:
        line = f"{self._prefix(index, result.step)} {self._mark(result)}"
        self._write(f"\r{line}{CLEAR_LINE}\n" if self._live else f"{line}\n")

    def summarise(self, results: Sequence[StepResult]) -> None:
        failed = [r for r in results if r.outcome is StepOutcome.FAILED]
        skipped = [r for r in results if r.outcome is StepOutcome.SKIPPED]
        for result in failed:
            self._write(f"\n── {result.step.title} failed: end of {result.log}\n")
            self._write("".join(f"    {line}\n" for line in result.tail))
        if not failed and not skipped:
            total = format_duration(sum(r.seconds for r in results))
            passed = f"Preflight passed: {len(results)} steps in {total}"
            self._write(f"\n{self._paint(GREEN, '✓')} {passed}\n")
            return
        titles = ", ".join(r.step.title for r in failed)
        line = f"Preflight failed: {titles} ({len(failed)} of {len(results)} steps)"
        if skipped:
            line += f", {len(skipped)} skipped"
        self._write(f"\n{self._paint(RED, '✗')} {line}. Fix it and rerun make preflight.\n")

    def _label(self, index: int, step: Step) -> str:
        return f"[{index}/{self._total}] {step.title}"

    def _prefix(self, index: int, step: Step) -> str:
        return f"{self._label(index, step)} ".ljust(self._width, ".")

    def _mark(self, result: StepResult) -> str:
        duration = f"{format_duration(result.seconds):>6}"
        if result.outcome is StepOutcome.PASSED:
            return f"{self._paint(GREEN, '✓ passed')} {duration}"
        if result.outcome is StepOutcome.FAILED:
            return f"{self._paint(RED, '✗ FAILED')} {duration}"
        return self._paint(DIM, "– skipped")

    def _paint(self, color: str, text: str) -> str:
        return f"{color}{text}{RESET}" if self._color else text

    def _write(self, text: str) -> None:
        self._stream.write(text)
        self._stream.flush()


def run_make_target(step: Step, log: Path, tick: Callable[[float], None]) -> StepRun:
    """Run `make <target>` with its output in `log`, calling `tick` with the elapsed seconds."""
    started = time.monotonic()
    last_tick = 0.0
    with log.open("w") as out:
        process = subprocess.Popen(
            ["make", "--no-print-directory", step.target],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed > STEP_TIMEOUT_SECONDS:
                    process.kill()
                    process.wait()
                    out.write(f"\npreflight: {step.target} timed out after {elapsed:.0f}s\n")
                    return StepRun(TIMED_OUT_EXIT, elapsed, read_tail(log))
                if elapsed - last_tick >= TICK_SECONDS:
                    last_tick = elapsed
                    tick(elapsed)
                time.sleep(POLL_SECONDS)
        except BaseException:
            process.kill()
            process.wait()
            raise
    return StepRun(process.returncode, time.monotonic() - started, read_tail(log))


def read_tail(log: Path) -> tuple[str, ...]:
    return tuple(log.read_text(errors="replace").splitlines()[-TAIL_LINES:])


def default_log_dir() -> Path:
    # Named after the checkout, so two worktrees running at once keep separate logs.
    return Path(tempfile.gettempdir()) / f"vecten-preflight-{Path.cwd().name}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=None, help="where each step's log goes")
    args = parser.parse_args(argv)
    log_dir: Path = args.log_dir or default_log_dir()
    shutil.rmtree(log_dir, ignore_errors=True)
    log_dir.mkdir(parents=True)
    terminal = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    color = terminal and "NO_COLOR" not in os.environ
    reporter = TerminalReporter(sys.stdout, color=color, live=terminal)
    try:
        results = run_preflight(STEPS, run_make_target, log_dir, reporter)
    except KeyboardInterrupt:
        sys.stdout.write("\npreflight: interrupted\n")
        return INTERRUPTED_EXIT
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
