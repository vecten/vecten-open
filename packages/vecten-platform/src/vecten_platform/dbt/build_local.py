"""The credential-free local build: replay fixtures, build and test dbt, regenerate datasets.

dbt parses every document under `osi-paths` on every command, and `osi/datasets.json` is an
output of this build, so the build runs against an empty Ossie directory and a separate
`dbt parse` validates the real `osi/` once the new datasets are in place.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from vecten_platform.dbt.local_warehouse import LocalTarget, local_target
from vecten_platform.dbt.raw_replay import missing_fixtures, replay_fixtures
from vecten_platform.dbt.runner import (
    OSI_PATH_VAR,
    DbtRun,
    area_vars,
    base_env,
    dbt_step,
    failed_nodes,
    output_tail,
    resolve_dependencies,
    run_build,
)
from vecten_shared.config import VectenConfig
from vecten_shared.osi.compiler import pyformat_statement
from vecten_shared.osi.evals import EVALS_PATH, check_values, load_cases
from vecten_shared.osi.generate import (
    GeneratorError,
    dataset_database,
    generate_datasets,
    render_document,
)
from vecten_shared.osi.issues import ModelError
from vecten_shared.osi.semantic import SemanticModel
from vecten_shared.osi.validate import is_fresh
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_record, write_text_atomic

RECORD_NAME = "dbt-build-local"
WORKDIR = Path(".vecten") / "dbt-local"
EMPTY_OSI = "osi-empty"
DATASETS = "osi/datasets.json"
DBT_LOG = "dbt/logs/dbt.log"
_ERROR_HEADER = re.compile(r"^\w[\w ]* Error in \w+ \S+ \(.+\)$")
_OSI_FILE = re.compile(r"OSI file '([^']+)'")
# Jinja's call stack and the compiled-SQL pointer follow the cause in a node's message.
_FRAMING = (">", "compiled code at")
_WAREHOUSES = {
    "postgres": "a disposable Postgres container",
    "snowflake": "the Snowflake emulation",
}


@dataclass(frozen=True)
class BuildOutcome:
    ok: bool
    kind: str
    replayed: dict[str, list[str]]
    failed: list[dict]
    datasets_changed: bool
    stale: bool
    datasets_sha256: str | None
    message: str
    evals: dict | None = None

    def as_data(self) -> dict:
        return asdict(self)

    def record(self) -> dict:
        return {
            "ok": self.ok,
            "warehouse_kind": self.kind,
            "replayed": self.replayed,
            "failed": self.failed,
            "datasets_changed": self.datasets_changed,
            "stale": self.stale,
            "datasets_sha256": self.datasets_sha256,
            "evals": self.evals,
        }


def _summary(message: str | None) -> str:
    """One line of a node's message: what went wrong, without dbt's framing around it."""
    lines = [line.strip() for line in (message or "").splitlines()]
    kept = [line for line in lines if line and not line.startswith(_FRAMING)]
    # The header repeats the node the summary is already keyed by; the cause is on later lines.
    if len(kept) > 1 and _ERROR_HEADER.match(kept[0]):
        kept = kept[1:]
    text = ""
    for line in kept:
        text += line if not text else (" " if text.endswith(":") else "; ") + line
    return text


def failure_detail(failed: list[dict], run: DbtRun) -> str:
    if failed:
        return "; ".join(f"{f['name']}: {_summary(f['message']) or f['status']}" for f in failed)
    return output_tail(run) or f"no failing node was recorded; see {DBT_LOG}"


def _fail(root: Path, outcome: BuildOutcome, code: ExitCode) -> Failure:
    write_record(root, RECORD_NAME, outcome.record())
    return Failure(code, outcome.message, details=outcome.as_data())


def _document_named(root: Path, text: str) -> str:
    """The project-relative path of the Ossie document dbt's message names, else `osi/`."""
    match = _OSI_FILE.search(text)
    if match is None:
        return "osi/"
    named = Path(match.group(1)).resolve()
    try:
        return named.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(named)


@dataclass(frozen=True)
class _Build:
    root: Path
    config: VectenConfig
    target: LocalTarget
    log: Callable[[str], None]
    replayed: dict[str, list[str]]

    @property
    def dbt_dir(self) -> Path:
        return self.root / "dbt"

    def outcome(self, **fields) -> BuildOutcome:
        base = {
            "ok": False,
            "kind": self.target.kind,
            "replayed": self.replayed,
            "failed": [],
            "datasets_changed": False,
            "stale": False,
            "datasets_sha256": None,
        }
        return BuildOutcome(**{**base, **fields})

    def red(self, run: DbtRun) -> Failure:
        failed = failed_nodes(self.dbt_dir)
        message = f"dbt failed: {failure_detail(failed, run)}"
        return _fail(self.root, self.outcome(failed=failed, message=message), ExitCode.TOOL_FAILED)


def _build_and_generate(build: _Build, env: dict[str, str]) -> str:
    """The rendered datasets document of a green build, or the Failure that stopped it."""
    dbt_dir = build.dbt_dir
    in_process = build.target.in_process
    build.log("resolving dbt dependencies")
    resolve_dependencies(project_dir=dbt_dir, config=build.config, env=env, in_process=in_process)
    build.log("building and testing the dbt project")
    # A build that fails before running any node writes no results, and an earlier build's
    # failures must not be reported in its place.
    (dbt_dir / "target" / "run_results.json").unlink(missing_ok=True)
    areas = build.config.areas
    last = run_build(project_dir=dbt_dir, env=env, in_process=in_process, areas=areas)[-1]
    if last.returncode != 0:
        raise build.red(last)
    build.log("generating the dbt catalog")
    docs = dbt_step(
        ["docs", "generate", *area_vars(areas)], project_dir=dbt_dir, env=env, in_process=in_process
    )
    if docs.returncode != 0:
        raise build.red(docs)

    build.log(f"generating {DATASETS}")
    target_dir = dbt_dir / "target"
    manifest = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
    catalog = json.loads((target_dir / "catalog.json").read_text(encoding="utf-8"))
    try:
        doc = generate_datasets(
            manifest,
            catalog,
            database=dataset_database(build.config),
            model_name=build.config.project.name.replace("-", "_"),
        )
    except GeneratorError as exc:
        lines = "\n".join(f"  {issue.render()}" for issue in exc.issues)
        message = f"{DATASETS} cannot be generated from the build:\n{lines}"
        raise _fail(build.root, build.outcome(message=message), ExitCode.CONFIG) from None
    return render_document(doc)


def _validate_osi(build: _Build, env: dict[str, str], outcome: BuildOutcome) -> None:
    """`dbt parse` over the real `osi/`: every dataset must match a model and carry an entity."""
    build.log("checking osi/ with dbt parse")
    run = dbt_step(
        ["parse"], project_dir=build.dbt_dir, env=env, in_process=build.target.in_process
    )
    if run.returncode == 0:
        return
    detail = failure_detail([], run)
    message = f"dbt cannot parse {_document_named(build.root, detail)}: {detail}"
    failed = BuildOutcome(**{**outcome.as_data(), "ok": False, "message": message})
    raise _fail(build.root, failed, ExitCode.CONFIG)


def _executor(target: LocalTarget) -> Callable[[str, list], tuple[list[str], list]]:
    """Runs one compiled query on the build's warehouse, which is still up."""
    if target.kind == "postgres":
        import psycopg2

        def execute(sql: str, parameters: list) -> tuple[list[str], list]:
            conn = psycopg2.connect(
                host=target.spec.host,
                port=target.spec.port,
                user=target.spec.role,
                password=target.password,
                dbname=target.spec.database,
            )
            try:
                with conn.cursor() as cur:
                    # psycopg2 %-formats the statement whenever parameters are passed, so
                    # literal percents are doubled then and left alone when nothing is bound.
                    if parameters:
                        cur.execute(pyformat_statement(sql), parameters)
                    else:
                        cur.execute(sql, None)
                    return [d.name for d in cur.description], cur.fetchall()
            finally:
                conn.close()

        return execute

    import snowflake.connector

    def execute(sql: str, parameters: list) -> tuple[list[str], list]:
        # The compiler binds Snowflake values with `?` placeholders.
        with snowflake.connector.connect(
            account=target.spec.account,
            user="fakesnow",
            password=target.password,
            database=target.spec.database,
            paramstyle="qmark",
        ) as conn:
            cur = conn.cursor()
            cur.execute(sql, parameters)
            return [d[0] for d in cur.description], cur.fetchall()

    return execute


def _check_eval_values(build: _Build, outcome: BuildOutcome) -> BuildOutcome:
    """Eval cases that carry an expected value run against the data this build produced."""
    path = build.root / EVALS_PATH
    if not path.is_file():
        return outcome
    try:
        cases = load_cases(path)
    except ModelError as exc:
        lines = "\n".join(f"  {issue.render()}" for issue in exc.issues)
        message = f"{EVALS_PATH.as_posix()} is malformed:\n{lines}"
        failed = replace(outcome, ok=False, message=message)
        raise _fail(build.root, failed, ExitCode.CONFIG) from None
    if not any(isinstance(case.expect.get("value"), dict) for case in cases):
        return outcome
    build.log("checking eval values against the built data")
    try:
        model = SemanticModel.load(build.root / "osi", kind=build.target.kind)
    except ModelError as exc:
        lines = "\n".join(f"  {issue.render()}" for issue in exc.issues)
        message = f"eval values need a valid osi/; run `vecten mcp validate`:\n{lines}"
        failed = replace(outcome, ok=False, message=message)
        raise _fail(build.root, failed, ExitCode.CONFIG) from None
    results = check_values(model, cases, _executor(build.target))
    failed = [r for r in results if not r.ok]
    evals = {"checked": len(results), "failed": [asdict(r) for r in failed]}
    outcome = replace(outcome, evals=evals)
    if failed:
        lines = "\n".join(f"  {r.render()}" for r in failed)
        message = f"eval values do not match the built data:\n{lines}"
        raise _fail(build.root, replace(outcome, ok=False, message=message), ExitCode.CONFIG)
    return outcome


def _build_on(
    target: LocalTarget,
    root: Path,
    config: VectenConfig,
    *,
    check: bool,
    quiet: bool,
    log: Callable[[str], None],
) -> BuildOutcome:
    log("replaying connector fixtures")
    replayed = replay_fixtures(target, root / "connectors", config)
    build = _Build(root, config, target, log, replayed)
    parse_env = base_env(build.dbt_dir, target.env)
    if quiet and target.in_process:
        # In-process dbt logs to this process's stdout, which carries the --json document;
        # the file log under dbt/logs keeps everything.
        parse_env["DBT_LOG_LEVEL"] = "none"
    empty_osi = target.workdir / EMPTY_OSI
    empty_osi.mkdir(parents=True, exist_ok=True)
    text = _build_and_generate(build, {**parse_env, OSI_PATH_VAR: str(empty_osi)})

    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    osi_dir = root / "osi"
    stale = not is_fresh(osi_dir, text)
    tables = sum(len(t) for t in replayed.values())
    built = f"built and tested on {_WAREHOUSES[target.kind]} from {tables} replayed tables"

    if check:
        if stale:
            message = f"{DATASETS} is stale; run `vecten dbt-build-local` and commit the result"
            outcome = build.outcome(ok=True, stale=True, datasets_sha256=sha, message=message)
            raise _fail(root, outcome, ExitCode.CONFIG)
        outcome = build.outcome(
            ok=True, datasets_sha256=sha, message=f"{built}; {DATASETS} is fresh"
        )
    else:
        if stale:
            osi_dir.mkdir(exist_ok=True)
            write_text_atomic(osi_dir / "datasets.json", text)
        state = "regenerated" if stale else "unchanged"
        outcome = build.outcome(
            ok=True,
            datasets_changed=stale,
            datasets_sha256=sha,
            message=f"{built}; {DATASETS} {state}",
        )
    _validate_osi(build, parse_env, outcome)
    outcome = _check_eval_values(build, outcome)
    write_record(root, RECORD_NAME, outcome.record())
    return outcome


def build_local(
    project_root: Path,
    config: VectenConfig,
    *,
    check: bool,
    log: Callable[[str], None],
    quiet: bool = False,
) -> BuildOutcome:
    """`quiet` keeps in-process dbt off stdout, which a `--json` run reserves for its document."""
    if not config.has("platform"):
        raise Failure(
            ExitCode.REFUSED,
            "dbt-build-local needs the platform component; this project has no dbt project",
        )
    missing = missing_fixtures(project_root / "connectors", config)
    if missing:
        names = ", ".join(missing)
        runs = " and ".join(f"`vecten connector run {name} --record`" for name in missing)
        raise Failure(ExitCode.PREREQUISITE, f"no fixtures for {names}; run {runs} first")

    workdir = project_root / WORKDIR
    shutil.rmtree(workdir, ignore_errors=True)
    try:
        log(f"starting {_WAREHOUSES[config.warehouse.kind]}")
        with local_target(config, workdir) as target:
            return _build_on(target, project_root, config, check=check, quiet=quiet, log=log)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
