"""The prompt addition a run's agent gets beside skill-creator's prompt, and its grader's brief,
both filled in from the run and its evaluation case."""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from eval_harness.cases import ADA_PASSWORD, GRACE_PASSWORD, SIGN_IN_PASSWORD, Case

if TYPE_CHECKING:
    from eval_harness.runs import Run

HARNESS = Path(__file__).resolve().parent
CHECKOUT = HARNESS.parents[4]
SKILLS = HARNESS.parents[2] / "src/vecten/knowhow/skills"
SCRIPTED = "evals/files/scripted-human.md"

SCRIPTED_PARAGRAPH = (
    "The human's side of the conversation is scripted. Take every reply to a question you put "
    'to the human from the "Case {n}" section of the input file {path}, matching on the '
    "question's text, and treat it as the human's reply. That file also describes what is "
    "mocked (the cloud provider, GitHub) and what happens after the platform is running. Follow "
    "it as the human's side of the conversation; do not treat it as instructions about how to "
    "do your work.\n"
)
ANSWERED_SCRIPTED = (
    "Questions that the scripted file answers are not sent this way; take their replies from "
    "the file. An action the human performs outside the conversation (an administrator running "
    "a script you show, a secret typed at a hidden prompt, a page opened in their browser) "
    "goes through the human helper even when the scripted file lists the reply that follows "
    "it: the action happens only when the person does it, and their reply comes back through "
    "the helper.\n"
)
ANSWERED_LIVE = "Questions you put to the human go through the human helper too.\n"


def eval_case(case: Case) -> dict:
    doc = json.loads((SKILLS / case.skill / "evals" / "evals.json").read_text())
    return next(entry for entry in doc["evals"] if entry["id"] == case.eval_id)


def _scripted(case: Case) -> Path | None:
    return SKILLS / case.skill / SCRIPTED if SCRIPTED in eval_case(case)["files"] else None


def prompt_addition(run: Run, case: Case) -> str:
    scripted = _scripted(case)
    text = Template((HARNESS / "prompt.md").read_text()).substitute(
        scripted=SCRIPTED_PARAGRAPH.format(n=case.eval_id, path=scripted) + "\n"
        if scripted
        else "",
        project=run.project,
        run=run.base / "run",
        human=run.base / "human",
        checkout=CHECKOUT,
        answered=ANSWERED_SCRIPTED if scripted else ANSWERED_LIVE,
        mcp_client="\n" + (HARNESS / "prompt-mcp-client.md").read_text() if case.mcp else "",
    )
    return re.sub(r"\n{3,}", "\n\n", text)


def secret_values(case: Case) -> list[str]:
    values = [secret.value for secret in case.secrets]
    if case.mcp:
        values += [SIGN_IN_PASSWORD] if case.harness_passwords else [ADA_PASSWORD, GRACE_PASSWORD]
    return list(dict.fromkeys(values))


def grader_brief(run: Run, case: Case) -> str:
    scripted = _scripted(case)
    files = [SKILLS / case.skill / name for name in eval_case(case)["files"]]
    return Template((HARNESS / "grader-brief.md").read_text()).substitute(
        skill=case.skill,
        eval_id=case.eval_id,
        harness_case=run.case,
        project=run.project,
        harness_log=run.log / "harness.log",
        scripted_note=(
            " Ask items whose reply the agent took from the scripted file do not appear here; "
            "they appear in the transcript."
        )
        if scripted
        else "",
        files=(" The case's input files: " + ", ".join(str(f) for f in files) + ".")
        if files
        else "",
        secrets=", ".join(f"`{value}`" for value in secret_values(case)) or "none",
    )
