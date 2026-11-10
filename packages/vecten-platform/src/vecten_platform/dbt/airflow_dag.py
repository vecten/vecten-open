"""The dbt DAG: build and test the shipped dbt project once every sync asset has updated.

Each step archives what dbt wrote under the run's keys in the artifact sink, so a failed run's
results outlive the next run that overwrites the target directory.

On the laptop stack the project directory is the host's checkout, so dbt's build output and
installed packages live in a container-local scratch directory, and dbt reads an empty Ossie
directory because `osi/datasets.json` is the output of a build, not its input.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path

from vecten_platform.dbt.artifacts import (
    BUILT_COMMIT_KEY,
    artifact_root,
    run_keys,
    sink_from_environ,
)
from vecten_platform.dbt.build_local import failure_detail
from vecten_platform.dbt.deps import CONVENTION_PACKAGE
from vecten_platform.dbt.profile import profile_env
from vecten_platform.dbt.runner import (
    BUILD_STEP,
    CONVENTION_STEP,
    OSI_PATH_VAR,
    DbtRun,
    base_env,
    failed_nodes,
    resolve_dependencies,
    run_dbt,
)
from vecten_platform.sync.airflow_dag import asset_for
from vecten_platform.sync.alerts import alert_on_failure
from vecten_platform.sync.context import RuntimeContext, runtime
from vecten_platform.sync.destination import loader_connection
from vecten_shared.config import VectenConfig, load_config

log = logging.getLogger(__name__)

DAG_ID = "dbt_build"
BUNDLE_COMMIT_VAR = "VECTEN_BUNDLE_COMMIT"
FRESHNESS_STEP = ("source", "freshness")
SCRATCH_DIR = Path("/tmp/vecten-dbt")
BUILD_TARGET = "target"
FRESHNESS_TARGET = "freshness"
_STALE = {"warn", "error", "runtime error"}


def dbt_assets(config: VectenConfig) -> list[tuple[str, str]]:
    return sorted((c.name, group) for c in config.connectors for group in c.groups)


def _dbt_dir(ctx: RuntimeContext) -> Path:
    return Path(ctx.project_root) / "dbt"


def target_dir(name: str = BUILD_TARGET) -> Path:
    return SCRATCH_DIR / name


def dbt_env_from_runtime(ctx: RuntimeContext, *, target: str = BUILD_TARGET) -> dict[str, str]:
    config = load_config(Path(ctx.project_root))
    spec, secret = loader_connection(ctx)
    if spec.kind != config.warehouse.kind:
        where = "the local stack" if ctx.env == "local" else f"environment {ctx.env}"
        raise RuntimeError(
            f"{where} offers a {spec.kind} connection but the project's warehouse is "
            f"{config.warehouse.kind}; the dbt DAG runs after deploy"
        )
    empty_osi = SCRATCH_DIR / "osi-empty"
    empty_osi.mkdir(parents=True, exist_ok=True)
    env = base_env(_dbt_dir(ctx), profile_env(spec, secret))
    return {
        **env,
        "DBT_TARGET_PATH": str(target_dir(target)),
        "DBT_PACKAGES_INSTALL_PATH": str(SCRATCH_DIR / "dbt_packages"),
        OSI_PATH_VAR: str(empty_osi),
    }


def _log_output(step: str, run: DbtRun) -> None:
    if run.stdout.strip():
        log.info("dbt %s output:\n%s", step, run.stdout)
    if run.stderr.strip():
        log.warning("dbt %s stderr:\n%s", step, run.stderr)


def install_deps() -> None:
    ctx = runtime()
    run = resolve_dependencies(
        project_dir=_dbt_dir(ctx),
        config=load_config(Path(ctx.project_root)),
        env=dbt_env_from_runtime(ctx),
        in_process=False,
    )
    _log_output("deps", run)


def ensure_deps() -> None:
    # The packages live in the container's scratch dir, which a ship's container recreation
    # empties, and Airflow retries only the failed step, not `deps`.
    if not (SCRATCH_DIR / "dbt_packages" / CONVENTION_PACKAGE).is_dir():
        install_deps()


def _run_and_archive(
    step: str, args: tuple[str, ...], output: str, key: str, target: str = BUILD_TARGET
) -> DbtRun:
    ensure_deps()
    ctx = runtime()
    env = dbt_env_from_runtime(ctx, target=target)
    produced = target_dir(target) / output
    # dbt leaves the file untouched when it fails before running any node, and an earlier
    # step's file must not be archived as this one's.
    produced.unlink(missing_ok=True)
    try:
        run = run_dbt(list(args), project_dir=_dbt_dir(ctx), env=env)
        _log_output(step, run)
    finally:
        if produced.is_file():
            _archive(key, produced)
    return run


def _archive(key: str, path: Path) -> None:
    try:
        sink_from_environ(os.environ).put_file(key, path)
    except Exception as exc:
        raise RuntimeError(f"results not archived to {artifact_root(os.environ)}: {exc}") from exc


def _raise_for_nodes(step: str, args: tuple[str, ...], run_id: str) -> None:
    run = _run_and_archive(step, args, "run_results.json", run_keys(run_id)[step])
    if run.returncode != 0:
        failed = failed_nodes(_dbt_dir(runtime()), target_dir=target_dir())
        raise RuntimeError(f"dbt {step} failed: {failure_detail(failed, run)}")


def build_step(run_id: str) -> None:
    _raise_for_nodes("build", BUILD_STEP, run_id)


def convention_step(run_id: str) -> None:
    _raise_for_nodes("convention", CONVENTION_STEP, run_id)


def _stale_sources(path: Path) -> list[str]:
    stale = []
    for result in json.loads(path.read_text(encoding="utf-8")).get("results", []):
        if result.get("status") in _STALE:
            # unique_id is `source.<package>.<source>.<table>`.
            name = ".".join(result["unique_id"].split(".")[2:])
            stale.append(f"{name}: {result['status']}")
    return stale


def freshness_check(run_id: str) -> None:
    """Staleness is recorded in the archived sources.json; only a missing record fails the task."""
    key = run_keys(run_id)["sources"]
    run = _run_and_archive("freshness", FRESHNESS_STEP, "sources.json", key, FRESHNESS_TARGET)
    produced = target_dir(FRESHNESS_TARGET) / "sources.json"
    if not produced.is_file():
        raise RuntimeError(f"dbt source freshness failed: {failure_detail([], run)}")
    for source in _stale_sources(produced):
        log.warning("stale source %s", source)


def mark_built() -> None:
    from vecten_shared.osi.revision import content_revision

    commit = os.environ.get(BUNDLE_COMMIT_VAR)
    if not commit:
        commit = content_revision(
            Path(os.environ.get("VECTEN_PROJECT_ROOT", "/opt/airflow")) / "osi"
        )
    marker = commit
    lineage = os.environ.get("VECTEN_BUNDLE_LINEAGE")
    if lineage:
        data = json.loads(Path(lineage).read_text())
        if not isinstance(data, dict) or data.get("commit") != commit:
            raise ValueError("build lineage does not match the current bundle")
        marker = json.dumps(data, sort_keys=True)
    sink_from_environ(os.environ).put_text(BUILT_COMMIT_KEY, f"{marker}\n")


def build_dbt_dag(
    assets: list[tuple[str, str]],
    *,
    retry_delay: timedelta = timedelta(minutes=10),  # noqa: B008
):
    # Airflow lives only in the Airflow image; importing it here keeps the task bodies light.
    from airflow.sdk import dag as dag_decorator
    from airflow.sdk import get_current_context, task

    @dag_decorator(
        dag_id=DAG_ID,
        schedule=[asset_for(c, g) for c, g in assets] or None,
        catchup=False,
        max_active_runs=1,
        default_args={
            "retries": 1,
            "retry_delay": retry_delay,
            "on_failure_callback": alert_on_failure,
        },
        tags=["vecten", "dbt"],
    )
    def dbt_dag():
        @task(task_id="deps")
        def deps() -> None:
            install_deps()

        @task(task_id="build")
        def build() -> None:
            build_step(get_current_context()["run_id"])

        @task(task_id="convention")
        def convention() -> None:
            convention_step(get_current_context()["run_id"])

        @task(task_id="freshness")
        def freshness() -> None:
            freshness_check(get_current_context()["run_id"])

        @task(task_id="mark_built")
        def built() -> None:
            mark_built()

        resolved = deps()
        resolved >> build() >> convention() >> built()
        # Stale sources are a likely cause of a red build, so the freshness record must not
        # depend on the build succeeding.
        resolved >> freshness()

    return dbt_dag()
