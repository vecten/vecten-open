"""Running dbt: as a subprocess of this interpreter, or in-process through `dbtRunner`."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vecten_platform.dbt.deps import CONVENTION_PACKAGE, write_packages_yml
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure

log = logging.getLogger(__name__)

# The convention's singular tests inspect the whole built graph, so they run after the build.
_CONVENTION_TESTS = f"package:{CONVENTION_PACKAGE},test_type:singular"
BUILD_STEP = ("build", "--exclude", _CONVENTION_TESTS)
CONVENTION_STEP = ("test", "--select", _CONVENTION_TESTS)
_BUILD_STEPS = (BUILD_STEP, CONVENTION_STEP)
_FAILED_STATUSES = {"error", "fail", "runtime error"}
# `dbt_project.yml` reads its `osi-paths` entry from this variable and falls back to `../osi`.
OSI_PATH_VAR = "VECTEN_OSI_PATH"
TAIL_CHARS = 2000


@dataclass(frozen=True)
class DbtRun:
    returncode: int
    stdout: str
    stderr: str


# Importing `dbt.cli` already imports `dbt.cli.main`, so `-m dbt.cli.main` makes Python warn
# that it runs a module twice; dbt has no `__main__`, so its click entry point is called.
_DBT_MAIN = "import sys; from dbt.cli.main import cli; sys.exit(cli(prog_name='dbt'))"


def dbt_argv(args: list[str]) -> list[str]:
    return [sys.executable, "-c", _DBT_MAIN, *args]


def base_env(
    project_dir: Path, profile: dict[str, str], inherited: Mapping[str, str] | None = None
) -> dict[str, str]:
    """dbt reads the root `osi/` unless a caller points `OSI_PATH_VAR` elsewhere explicitly.

    `inherited` is the environment passed on beneath the settings here, `os.environ` by default.
    """
    source = os.environ if inherited is None else inherited
    inherited = {k: v for k, v in source.items() if k != OSI_PATH_VAR}
    return {
        **inherited,
        "DBT_PROJECT_DIR": str(project_dir),
        "DBT_PROFILES_DIR": str(project_dir),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "0",
        "DBT_LOG_PATH": str(project_dir / "logs"),
        # dbt's file log follows the console level unless set apart, and a quiet console must
        # still leave the log that failure messages point at.
        "DBT_LOG_LEVEL_FILE": "debug",
        **profile,
    }


def output_tail(run: DbtRun, limit: int = TAIL_CHARS) -> str:
    """The last `limit` characters of each stream; dbt reports its errors on either."""
    return "\n".join(text.strip()[-limit:] for text in (run.stdout, run.stderr) if text.strip())


def run_dbt(
    args: list[str], *, project_dir: Path, env: dict[str, str], capture: bool = True
) -> DbtRun:
    proc = subprocess.run(
        dbt_argv(args), cwd=project_dir, env=env, capture_output=capture, text=True, check=False
    )
    return DbtRun(proc.returncode, proc.stdout or "", proc.stderr or "")


@contextmanager
def _process_state(project_dir: Path, env: dict[str, str]) -> Iterator[None]:
    saved_env = dict(os.environ)
    saved_cwd = Path.cwd()
    os.chdir(project_dir)
    try:
        os.environ.clear()
        os.environ.update(env)
        yield
    finally:
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)


def _close_file_log() -> None:
    # dbt drops its file handler on the next invocation without closing the file.
    file_log = logging.getLogger("file_log")
    for handler in list(file_log.handlers):
        file_log.removeHandler(handler)
        handler.close()


def invoke_dbt(args: list[str], *, project_dir: Path, env: dict[str, str]) -> DbtRun:
    """dbt in this process; an error that stops dbt before any node runs lands in `stderr`."""
    from dbt.cli.main import dbtRunner

    with _process_state(project_dir, env):
        try:
            res = dbtRunner().invoke(args)
        finally:
            _close_file_log()
    if res.exception is not None:
        log.error("dbt %s raised: %s", " ".join(args), res.exception)
        return DbtRun(1, "", str(res.exception))
    return DbtRun(0 if res.success else 1, "", "")


def dbt_step(
    args: list[str], *, project_dir: Path, env: dict[str, str], in_process: bool
) -> DbtRun:
    if in_process:
        return invoke_dbt(args, project_dir=project_dir, env=env)
    return run_dbt(args, project_dir=project_dir, env=env)


def run_build(*, project_dir: Path, env: dict[str, str], in_process: bool) -> list[DbtRun]:
    runs: list[DbtRun] = []
    for args in _BUILD_STEPS:
        run = dbt_step(list(args), project_dir=project_dir, env=env, in_process=in_process)
        runs.append(run)
        if run.returncode != 0:
            break
    return runs


def _node_name(unique_id: str) -> str:
    # unique_id is `<resource_type>.<package>.<name>[.<version or test hash>]`.
    parts = unique_id.split(".")
    return parts[2] if len(parts) > 2 else parts[-1]


def failed_nodes(project_dir: Path, *, target_dir: Path | None = None) -> list[dict]:
    path = (target_dir or project_dir / "target") / "run_results.json"
    if not path.is_file():
        return []
    results = json.loads(path.read_text()).get("results", [])
    return [
        {
            "unique_id": r["unique_id"],
            "name": _node_name(r["unique_id"]),
            "status": r["status"],
            "message": r.get("message"),
        }
        for r in results
        if r.get("status") in _FAILED_STATUSES
    ]


def resolve_dependencies(
    *, project_dir: Path, config: VectenConfig, env: dict[str, str], in_process: bool
) -> DbtRun:
    write_packages_yml(project_dir, config)
    run = dbt_step(["deps"], project_dir=project_dir, env=env, in_process=in_process)
    if run.returncode != 0:
        detail = output_tail(run) or f"see {project_dir / 'logs' / 'dbt.log'}"
        raise Failure(ExitCode.TOOL_FAILED, f"dbt deps failed: {detail}")
    return run
