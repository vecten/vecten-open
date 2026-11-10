"""One evaluation run: a harness base of its own, the helpers its agent calls, and the person's
side of the channel between them.

A run's base sits in the checkout (`.eval-runs/<id>/` by default), the one durable place a
sandboxed Claude Code session started there may write, so the agent's wrapped commands, their
log and the inbox need no sandbox exception for files.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from eval_harness.cases import Case

HARNESS = Path(__file__).resolve().parent
CHECKOUT = HARNESS.parents[4]
DEFAULT_RUNS = CHECKOUT / ".eval-runs"
DEMO_API_DOC = HARNESS.parents[2] / "src/vecten/knowhow/skills/connector/evals/files/demo-api.md"


@dataclass(frozen=True)
class Run:
    base: Path
    case: str

    @property
    def id(self) -> str:
        return self.base.name

    @property
    def case_dir(self) -> Path:
        return self.base / self.case

    @property
    def env_file(self) -> Path:
        return self.case_dir / "env.sh"

    @property
    def project(self) -> Path:
        return self.case_dir / "proj"

    @property
    def log(self) -> Path:
        return self.base / "log"

    @property
    def inbox(self) -> Path:
        return self.base / "inbox"

    @property
    def bin(self) -> Path:
        return self.base / "bin"

    @property
    def client_state(self) -> Path:
        return self.base / "client-state"

    @property
    def prompt_file(self) -> Path:
        return self.base / "prompt.md"


def _render(template: str, target: Path, values: dict[str, str]) -> None:
    text = (HARNESS / template).read_text()
    for name, value in values.items():
        text = text.replace(f"@@{name}@@", shlex.quote(value))
    target.write_text(text)
    target.chmod(0o755)


def write_helpers(run: Run, case: Case, python: Path = Path(sys.executable)) -> None:
    """Write the run's helpers; the log and the inbox of an earlier start are kept."""
    for directory in (run.project, run.log, run.inbox, run.bin):
        directory.mkdir(parents=True, exist_ok=True)
    _render(
        "run.sh",
        run.base / "run",
        {
            "BASE": str(run.base),
            "PYBIN": str(python.parent),
            "DEMO_DOC": str(DEMO_API_DOC) if case.demo_doc else "",
            "ENV": str(run.env_file),
            "PROJECT": str(run.project),
        },
    )
    _render(
        "human.sh", run.base / "human", {"INBOX": str(run.inbox), "HUMAN": str(run.base / "human")}
    )
    from eval_harness.prompts import prompt_addition

    run.prompt_file.write_text(prompt_addition(run, case))
    _render(
        "mcp-client.sh",
        run.bin / "mcp-client",
        {
            "STATE": str(run.client_state),
            "PYTHON": str(python),
            "SCRIPT": str(HARNESS / "mcp_client.py"),
        },
    )


def find_run(runs_dir: Path, run_id: str) -> Run:
    """The run laid out under `runs_dir`; its case is the one folder holding an `env.sh`."""
    base = (runs_dir.expanduser() / run_id).resolve()
    found = sorted(base.glob("*/env.sh"))
    if len(found) != 1:
        raise SystemExit(f"{base} is not one run laid out by `up` or `env` with --run")
    return Run(base, found[0].parent.name)


@dataclass(frozen=True)
class Ask:
    run: str
    number: str
    text: str
    reply: str | None

    def line(self) -> str:
        return f"ASK {self.run} {self.number}: {' '.join(self.text[:300].split())}"


def asks(runs_dir: Path) -> list[Ask]:
    found = []
    for path in sorted(runs_dir.glob("*/inbox/*.ask")):
        reply = path.with_suffix(".reply")
        found.append(
            Ask(
                path.parent.parent.name,
                path.stem,
                path.read_text(),
                reply.read_text() if reply.exists() else None,
            )
        )
    return found


def follow_asks(runs_dir: Path, emit, rounds: int | None = None, sleep=time.sleep) -> None:
    """Emit every unanswered ask, then each new one as it arrives."""
    seen: set[tuple[str, str]] = set()
    done = 0
    while rounds is None or done < rounds:
        for found in asks(runs_dir):
            key = (found.run, found.number)
            if key not in seen and found.reply is None:
                emit(found)
            seen.add(key)
        done += 1
        sleep(1)


def reply(run: Run, number: str, text: str) -> None:
    if not (run.inbox / f"{number}.ask").exists():
        raise ValueError(f"run {run.id} has no message {number}")
    staged = run.inbox / f"{number}.tmp"
    staged.write_text(text + "\n")
    staged.rename(run.inbox / f"{number}.reply")


def teardown(run: Run, environ: dict[str, str]) -> int:
    """`vecten down --volumes` in the run's project, kept out of the agent's command log."""
    from eval_harness.issues_replay import real_vecten

    if not (run.project / "vecten.toml").exists():
        print(f"{run.project} holds no project; nothing to stop")
        return 0
    command = [*real_vecten(environ.get("PATH", "")), "down", "--volumes"]
    result = subprocess.run(
        command, cwd=run.project, env=environ, capture_output=True, text=True, check=False
    )
    run.log.mkdir(parents=True, exist_ok=True)
    (run.log / "teardown.out").write_text(result.stdout + result.stderr)
    print(
        f"`vecten down --volumes` exited {result.returncode}; output in {run.log / 'teardown.out'}"
    )
    return result.returncode
