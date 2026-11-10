"""From a finished run to the skill-creator benchmark: the grader's evidence, the grades, the
benchmark's placeholders and the GitHub report."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import cli, grading, runs  # noqa: E402

CONFIGS = ("with_skill", "without_skill")


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def workspace(tmp_path):
    """One iteration of one eval, before `prepare-benchmark` moved the runs into run-1/."""
    iteration = tmp_path / "ws" / "iteration-1"
    case = iteration / "eval-full-platform-deploy"
    write_json(
        case / "eval_metadata.json",
        {"eval_id": 1, "eval_name": "full-platform-deploy", "prompt": "p", "assertions": []},
    )
    for config in CONFIGS:
        write_json(
            case / config / "grading.json",
            {
                "expectations": [
                    {"text": "ran next", "passed": True, "evidence": "001 vecten next"},
                    {"text": "no secret", "passed": False, "evidence": "a | b in transcript"},
                ],
                "timing": {"x": 1},
            },
        )
        write_json(
            case / config / "timing.json", {"total_duration_seconds": 61.0, "total_tokens": 1234}
        )
        (case / config / "outputs").mkdir()
    summary = {
        config: {"pass_rate": {"mean": 0.5}, "time_seconds": {"mean": 61}, "tokens": {"mean": 1234}}
        for config in CONFIGS
    }
    summary["delta"] = {"pass_rate": "+0.00", "time_seconds": "+0", "tokens": "+0"}
    write_json(
        iteration / "benchmark.json", {"metadata": {"skill_name": "guide"}, "run_summary": summary}
    )
    (iteration / "benchmark.md").write_text(
        "**Model**: <model-name>\n\nResults (3 runs each per configuration)\n"
    )
    return iteration


def test_bundle_writes_commands_and_messages(tmp_path):
    run = runs.Run(tmp_path / "r1", "guide-1")
    run.log.mkdir(parents=True)
    run.inbox.mkdir()
    (run.log / "001.cmd").write_text("vecten next --json\n")
    (run.log / "001.out").write_text("x" * 2000)
    (run.log / "001.rc").write_text("0\n")
    (run.log / "002.cmd").write_text("mcp-client call https://m/mcp query_metrics '{}'\n")
    (run.log / "002.out").write_text("y" * 2000)
    (run.inbox / "001.ask").write_text("Please type the secret\n")
    (run.inbox / "001.reply").write_text("done\n")
    (run.inbox / "002.ask").write_text("Still there?\n")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    assert grading.bundle(run, outputs) == 2
    commands = (outputs / "commands.md").read_text()
    assert "## 001  exit 0" in commands and "(2000 chars in all)" in commands
    assert "## 002  exit (still running or killed)" in commands and "y" * 2000 in commands
    messages = (outputs / "human-messages.md").read_text()
    assert "Please type the secret" in messages and "done" in messages
    assert messages.count("(none)") == 1


def test_prepare_benchmark_moves_runs_and_recomputes_summaries(tmp_path):
    iteration = workspace(tmp_path)
    assert len(grading.prepare_benchmark(iteration.parent)) == 2
    run = iteration / "eval-full-platform-deploy" / "with_skill" / "run-1"
    doc = json.loads((run / "grading.json").read_text())
    assert doc["summary"] == {"passed": 1, "failed": 1, "total": 2, "pass_rate": 0.5}
    assert "timing" not in doc
    assert (run / "timing.json").is_file() and (run / "outputs").is_dir()
    assert len(grading.prepare_benchmark(iteration.parent)) == 2
    assert not (run / "run-1").exists()


def test_grades_flags_missing_fields(tmp_path):
    iteration = workspace(tmp_path)
    broken = iteration / "eval-full-platform-deploy" / "without_skill" / "grading.json"
    write_json(broken, {"expectations": [{"text": "t", "passed": True}]})
    lines = grading.grades(iteration.parent)
    assert any("with_skill" in line and "1/2" in line and "fields ok" in line for line in lines)
    assert any("without_skill" in line and "fields BAD" in line for line in lines)


def test_finish_benchmark_fills_the_placeholders(tmp_path):
    iteration = workspace(tmp_path)
    grading.finish_benchmark(
        iteration,
        executor="claude-x",
        grader="claude-y",
        skill=Path("/s/guide/SKILL.md"),
        commit="abc1234",
        notes=["one run each"],
    )
    doc = json.loads((iteration / "benchmark.json").read_text())
    assert doc["metadata"]["executor_model"] == "claude-x"
    assert doc["metadata"]["analyzer_model"] == "claude-y"
    assert doc["metadata"]["skill_path"] == "/s/guide/SKILL.md"
    assert doc["metadata"]["runs_per_configuration"] == 1
    assert doc["metadata"]["vecten_commit"] == "abc1234"
    assert doc["notes"] == ["one run each"]
    md = (iteration / "benchmark.md").read_text()
    assert "**Model**: claude-x (graded by claude-y)" in md
    assert "(1 run each per configuration)" in md


def test_report_titles_cases_from_their_metadata(tmp_path):
    iteration = workspace(tmp_path)
    grading.prepare_benchmark(iteration.parent)
    text = grading.report(iteration, executor="claude-x", grader="claude-y", commit="abc1234")
    assert "### Case 1: full-platform-deploy" in text
    assert "| 1 | ✅ | ran next |" in text and "| 2 | ❌ | no secret |" in text
    assert "a \\| b in transcript" in text
    assert "| with the skill | 50.0% | 61 s | 1,234 |" in text


def test_the_cli_writes_the_report_and_reads_notes_from_a_file(tmp_path):
    iteration = workspace(tmp_path)
    assert cli.main(["prepare-benchmark", str(iteration.parent)]) == 0
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps(["a note"]))
    finish = ["finish-benchmark", str(iteration), "--executor", "claude-x", "--grader", "claude-y"]
    finish += ["--skill", "/s/SKILL.md", "--commit", "abc1234", "--notes", str(notes)]
    assert cli.main(finish) == 0
    out = tmp_path / "report.md"
    report = ["report", str(iteration), "--executor", "claude-x", "--grader", "claude-y"]
    assert cli.main([*report, "--commit", "abc1234", "--out", str(out)]) == 0
    assert "- a note" in out.read_text()
