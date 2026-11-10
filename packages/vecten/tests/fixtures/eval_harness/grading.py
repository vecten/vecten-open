"""From finished runs to the skill-creator benchmark and a GitHub report.

The workspace is skill-creator's: `<workspace>/iteration-<N>/eval-<name>/<config>/` per run,
`eval_metadata.json` per eval, `benchmark.json` and `benchmark.md` per iteration once its
aggregator ran. The aggregator reads one `run-<k>/` folder per run, which skill-creator's
subagent runs do not create when each configuration runs once.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from eval_harness.runs import Run

OUTPUT_LIMIT = 1500
EVIDENCE_LIMIT = 420
CONFIGS = (("with_skill", "with the skill"), ("without_skill", "without the skill"))


def bundle(run: Run, outputs: Path) -> int:
    """Write the grader's `commands.md` and `human-messages.md`; the commands bundled."""
    commands = sorted(run.log.glob("*.cmd"))
    lines = ["# Commands the agent ran through the run wrapper, in order", ""]
    for cmd in commands:
        rc_file, out_file = cmd.with_suffix(".rc"), cmd.with_suffix(".out")
        rc = rc_file.read_text().strip() if rc_file.exists() else "(still running or killed)"
        out = out_file.read_text(errors="replace") if out_file.exists() else ""
        command = cmd.read_text().strip()
        # A tool result is evidence in full: the grader checks figures and receipts against it.
        if "mcp-client call" not in command and len(out) > OUTPUT_LIMIT:
            out = out[:OUTPUT_LIMIT] + f"\n... ({len(out)} chars in all)"
        lines += [f"## {cmd.stem}  exit {rc}", "```", command, "```", "Output:", "```"]
        lines += [out.rstrip(), "```", ""]
    (outputs / "commands.md").write_text("\n".join(lines))
    messages = ["# Messages the agent put to the person through the human helper", ""]
    for ask in sorted(run.inbox.glob("*.ask")):
        reply = ask.with_suffix(".reply")
        answer = reply.read_text().strip() if reply.exists() else "(none)"
        messages += [f"## {ask.stem}", "Agent:", "```", ask.read_text().strip(), "```"]
        messages += ["Reply:", "```", answer, "```", ""]
    (outputs / "human-messages.md").write_text("\n".join(messages))
    return len(commands)


def _score(doc: dict) -> tuple[int, int]:
    items = doc["expectations"]
    return sum(1 for item in items if item["passed"]), len(items)


def grades(workspace: Path) -> list[str]:
    """One line per graded run: its score and whether the viewer's fields are all there."""
    lines = []
    for path in sorted(workspace.rglob("grading.json")):
        doc = json.loads(path.read_text())
        fields = all({"text", "passed", "evidence"} <= set(i) for i in doc["expectations"])
        passed, total = _score(doc)
        where = "/".join(path.parent.relative_to(workspace).parts)
        ok = "ok" if fields else "BAD"
        lines.append(f"{where:60} {passed}/{total}  fields {ok}  summary {'summary' in doc}")
    return lines


def prepare_benchmark(workspace: Path) -> list[str]:
    """Move each configuration's run into `run-1/` and recompute its grading summary."""
    lines = []
    for config in sorted(workspace.glob("iteration-*/eval-*/*_skill")):
        run = config / "run-1"
        if not run.exists():
            run.mkdir()
            for item in list(config.iterdir()):
                if item.name != "run-1":
                    shutil.move(str(item), run / item.name)
        grading = run / "grading.json"
        doc = json.loads(grading.read_text())
        passed, total = _score(doc)
        summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
        doc["summary"] = {
            **summary,
            "passed": passed,
            "failed": total - passed,
            "total": total,
            "pass_rate": round(passed / total, 4),
        }
        # A timing block in the grades would override the run's own timing for the aggregator.
        doc.pop("timing", None)
        grading.write_text(json.dumps(doc, indent=2) + "\n")
        lines.append(f"{run.relative_to(workspace)} {passed}/{total}")
    return lines


def finish_benchmark(
    iteration: Path, *, executor: str, grader: str, skill: Path, commit: str, notes: list[str]
) -> None:
    """Fill in what the aggregator leaves as placeholders."""
    path = iteration / "benchmark.json"
    doc = json.loads(path.read_text())
    doc["metadata"].update(
        executor_model=executor,
        analyzer_model=grader,
        skill_path=str(skill),
        runs_per_configuration=1,
        vecten_commit=commit,
    )
    doc["notes"] = notes
    path.write_text(json.dumps(doc, indent=2) + "\n")
    md_path = iteration / "benchmark.md"
    md = md_path.read_text().replace(
        "**Model**: <model-name>", f"**Model**: {executor} (graded by {grader})"
    )
    md = re.sub(r"\(\d+ runs each per configuration\)", "(1 run each per configuration)", md)
    md_path.write_text(md)


def _clip(text) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= EVIDENCE_LIMIT else text[: EVIDENCE_LIMIT - 1] + "…"


def report(iteration: Path, *, executor: str, grader: str, commit: str) -> str:
    """The per-run results of one iteration as GitHub markdown."""
    bench = json.loads((iteration / "benchmark.json").read_text())
    summary = bench["run_summary"]
    lines = [
        f"## Detailed results: `{executor}`",
        "",
        f"Executor `{executor}`; grader `{grader}`. Commit: {commit}. One run per case and "
        "configuration. Time includes the minutes the agent waited for the person's replies.",
        "",
        "| Configuration | Pass rate | Mean time | Mean tokens |",
        "|---|---|---|---|",
    ]
    for key, label in CONFIGS:
        s = summary[key]
        lines.append(
            f"| {label} | {s['pass_rate']['mean'] * 100:.1f}% | {s['time_seconds']['mean']:.0f} s "
            f"| {s['tokens']['mean']:,.0f} |"
        )
    delta = summary["delta"]
    lines += [
        "",
        f"Delta: pass rate {delta['pass_rate']}, time {delta['time_seconds']}, "
        f"tokens {delta['tokens']}.",
        "",
    ]
    for case in sorted(iteration.glob("eval-*")):
        meta = json.loads((case / "eval_metadata.json").read_text())
        lines += [f"### Case {meta['eval_id']}: {meta['eval_name']}", ""]
        for key, label in CONFIGS:
            run = case / key / "run-1"
            items = json.loads((run / "grading.json").read_text())["expectations"]
            timing = json.loads((run / "timing.json").read_text())
            passed = sum(1 for item in items if item["passed"])
            lines += [
                "<details>",
                f"<summary><b>{label}</b>: {passed}/{len(items)} · "
                f"{timing['total_duration_seconds']:.0f} s · "
                f"{timing['total_tokens']:,} tokens</summary>",
                "",
                "| # | Result | Expectation | Evidence |",
                "|---|---|---|---|",
            ]
            for n, item in enumerate(items, 1):
                mark = "✅" if item["passed"] else "❌"
                lines.append(
                    f"| {n} | {mark} | {_clip(item['text'])} | {_clip(item['evidence'])} |"
                )
            if timing.get("note"):
                lines += ["", f"Timing note: {timing['note']}"]
            lines += ["", "</details>", ""]
    lines += ["### Analyst notes", ""] + [f"- {note}" for note in bench.get("notes", [])]
    return "\n".join(lines) + "\n"
