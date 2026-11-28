import importlib.util
import io
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[3] / "scripts" / "preflight.py"


@pytest.fixture(scope="module")
def preflight():
    spec = importlib.util.spec_from_file_location("preflight", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules while the class is created.
    sys.modules["preflight"] = module
    spec.loader.exec_module(module)
    yield module
    del sys.modules["preflight"]


def fake_runner(preflight, codes):
    """Runs nothing: each target exits with its code from `codes`, after 2 s, logging one line."""
    calls = []

    def run(step, log, tick):
        calls.append(step.target)
        tick(1.0)
        return preflight.StepRun(
            exit_code=codes.get(step.target, 0), seconds=2.0, tail=(f"{step.target} output",)
        )

    return run, calls


def plain(preflight):
    stream = io.StringIO()
    return preflight.TerminalReporter(stream, color=False, live=False), stream


def test_every_step_passing_reports_each_and_a_passing_summary(preflight, tmp_path):
    run, calls = fake_runner(preflight, {})
    reporter, stream = plain(preflight)
    results = preflight.run_preflight(preflight.STEPS, run, tmp_path, reporter)
    assert calls == [s.target for s in preflight.STEPS]
    assert all(r.outcome is preflight.StepOutcome.PASSED for r in results)
    lines = stream.getvalue().splitlines()
    assert lines[0] == f"Preflight: CI's lint and unit jobs (logs in {tmp_path})"
    assert lines[2].startswith("[1/6] Sync the lock ") and lines[2].endswith("✓ passed   2.0s")
    assert lines[-1] == "✓ Preflight passed: 6 steps in 12.0s"


def test_a_failing_step_lets_the_rest_run_and_shows_its_log_tail(preflight, tmp_path):
    run, calls = fake_runner(preflight, {"lint": 1})
    reporter, stream = plain(preflight)
    results = preflight.run_preflight(preflight.STEPS, run, tmp_path, reporter)
    assert calls == [s.target for s in preflight.STEPS]
    assert preflight.exit_code(results) == 1
    out = stream.getvalue()
    assert "✗ FAILED   2.0s" in out
    assert f"── Ruff lint and format failed: end of {tmp_path / 'lint.log'}" in out
    assert "    lint output\n" in out
    assert out.splitlines()[-1] == (
        "✗ Preflight failed: Ruff lint and format (1 of 6 steps). Fix it and rerun make preflight."
    )


def test_a_failed_sync_skips_every_later_step(preflight, tmp_path):
    run, calls = fake_runner(preflight, {"sync": 2})
    reporter, stream = plain(preflight)
    results = preflight.run_preflight(preflight.STEPS, run, tmp_path, reporter)
    assert calls == ["sync"]
    assert [r.outcome for r in results[1:]] == [preflight.StepOutcome.SKIPPED] * 5
    assert "– skipped" in stream.getvalue()
    assert "5 skipped" in stream.getvalue().splitlines()[-1]


def test_a_live_terminal_redraws_the_line_with_the_elapsed_time(preflight, tmp_path):
    run, _ = fake_runner(preflight, {})
    stream = io.StringIO()
    reporter = preflight.TerminalReporter(stream, color=True, live=True)
    preflight.run_preflight(preflight.STEPS[:1], run, tmp_path, reporter)
    out = stream.getvalue()
    assert "\r[1/1] Sync the lock" in out and "1.0s" in out
    assert "\x1b[32m✓ passed\x1b[0m" in out


@pytest.mark.parametrize(
    ("seconds", "text"), [(0.04, "0.0s"), (12.34, "12.3s"), (59.96, "1m00s"), (259.2, "4m19s")]
)
def test_durations_read_as_seconds_then_minutes(preflight, seconds, text):
    assert preflight.format_duration(seconds) == text


def test_the_steps_are_makefile_targets(preflight):
    makefile = (SCRIPT.parents[1] / "Makefile").read_text()
    for step in preflight.STEPS:
        assert f"\n{step.target}:" in makefile, step.target
