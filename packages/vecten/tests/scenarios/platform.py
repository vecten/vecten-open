"""The hosts of a scenario, run in this process from the releases the mocked cloud placed.

The platform host loads raw data and runs the dbt DAG's own task bodies; the MCP host serves
the configured runtime with the environment its container gets from the bundle. Both read the
release directory only, as a VM does once the agent has extracted a bundle.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec

SERVED = Path(__file__).parents[3] / "vecten-mcp/tests/served.py"
DEPLOYED_AIRFLOW = "https://airflow.data.acme.com"
LAPTOP_AIRFLOW = "http://127.0.0.1:8482"
MOCK_CLOUD_DIR = Path(__file__).parents[1] / "fixtures" / "mock_cloud"


def use_mock_cloud(monkeypatch, cloud_dir: Path) -> None:
    """Point every control plane in this process at the mocked cloud in `cloud_dir`."""
    from vecten_shared.provider import _REGISTRY

    monkeypatch.syspath_prepend(str(MOCK_CLOUD_DIR))
    monkeypatch.setenv("VECTEN_CONTROL_PLANE", "vecten_mock_cloud:MockCloud")
    monkeypatch.setenv("VECTEN_MOCK_CLOUD_DIR", str(cloud_dir))
    monkeypatch.setattr("vecten_shared.provider._REGISTRY", dict(_REGISTRY))


def load_served():
    """The MCP test harness, which lives with the MCP package's tests."""
    if "scenario_served" not in sys.modules:
        spec = importlib.util.spec_from_file_location("scenario_served", SERVED)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules["scenario_served"]


class LoaderProvider:
    """What the platform VM's provider answers for the warehouse, on the laptop's Postgres."""

    def __init__(self, pg):
        self.pg = pg

    def connection(self, env, db):
        return ConnectionSpec(
            kind="postgres",
            database=db,
            role="loader",
            auth="password",
            host=self.pg.host,
            port=self.pg.port,
        )

    def get_secret(self, name):
        if name != "LOADER_PASSWORD":
            raise Failure(ExitCode.PREREQUISITE, f"secret {name} is not set")
        return self.pg.passwords["LOADER_PASSWORD"]


def bundle_environ(release: Path, artifacts: Path) -> dict[str, str]:
    """The variables a host's containers get from the bundle they run."""
    ship = release / ".vecten" / "ship"
    return {
        "VECTEN_BUNDLE_COMMIT": release.name,
        "VECTEN_BUNDLE_LINEAGE": str(ship / "build-lineage.json"),
        "VECTEN_FEEDBACK_MANIFEST": str(ship / "feedback.json"),
        "VECTEN_ARTIFACT_DIR": str(artifacts),
    }


class Hosts:
    """Both hosts of the mocked environment over one Postgres instance, or the laptop stack
    when `laptop` names the project checkout the local Airflow runs from.

    Each run is recorded in `airflow`, the Airflow REST API `vecten issues` reads: one night per
    sync, with the failed task's log where that stack keeps it.
    """

    def __init__(self, cloud_dir: Path, pg, monkeypatch, scratch: Path, laptop: Path | None = None):
        from .airflow import FakeAirflow

        self.cloud_dir, self.pg, self.monkeypatch = cloud_dir, pg, monkeypatch
        self.scratch, self.laptop = scratch, laptop
        if laptop is None:
            # The mocked bucket's `platform/` prefix, where a VM's containers write.
            self.artifacts = cloud_dir / "bucket" / "platform"
            self.airflow = FakeAirflow(
                DEPLOYED_AIRFLOW, log_dir=self.artifacts / "logs", api_logs=False
            )
        else:
            self.artifacts = laptop / ".vecten" / "artifacts"
            self.airflow = FakeAirflow(LAPTOP_AIRFLOW)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.night = datetime.now(UTC).replace(hour=2, minute=0, second=0, microsecond=0)
        self.night -= timedelta(days=10)

    def release(self, host: str) -> Path:
        if self.laptop is not None:
            return self.laptop
        from vecten_mock_cloud import MockCloud

        return MockCloud.release(self.cloud_dir, host)

    def _next_night(self) -> tuple[str, datetime]:
        self.night += timedelta(days=1)
        return f"scheduled__{self.night.isoformat()}", self.night

    def _environ(self, host: str) -> dict[str, str]:
        environ = bundle_environ(self.release(host), self.artifacts)
        for name, value in environ.items():
            self.monkeypatch.setenv(name, value)
        return environ

    def load_raw(self) -> dict[str, list[str]]:
        """Replay the release's connector fixtures into `raw_<name>`, as the sync DAG lands it."""
        from vecten_platform.dbt.local_warehouse import LocalTarget
        from vecten_platform.dbt.profile import profile_env
        from vecten_platform.dbt.raw_replay import replay_fixtures

        release = self.release("platform")
        provider = LoaderProvider(self.pg)
        spec = provider.connection("prod", "warehouse")
        password = provider.get_secret("LOADER_PASSWORD")
        target = LocalTarget(
            kind="postgres",
            spec=spec,
            password=password,
            env=profile_env(spec, password),
            in_process=False,
            workdir=self.scratch / "replay",
        )
        return replay_fixtures(target, release / "connectors", load_config(release))

    @contextmanager
    def platform_runtime(self):
        from vecten_platform.dbt import airflow_dag
        from vecten_platform.sync.context import RuntimeContext, set_runtime

        release = self.release("platform")
        self._environ("platform")
        self.monkeypatch.setattr(airflow_dag, "SCRATCH_DIR", self.scratch / "dbt")
        set_runtime(
            RuntimeContext(
                project_root=release,
                connectors_dir=release / "connectors",
                env="prod",
                provider=LoaderProvider(self.pg),
                dlt_dir=self.scratch / "dlt",
            )
        )
        try:
            yield release
        finally:
            set_runtime(None)

    def dbt_run(self) -> str:
        """The dbt DAG's tasks in order, from the platform host's release; its commit."""
        from vecten_platform.dbt import airflow_dag

        with self.platform_runtime() as release:
            run_id = f"scenario__{release.name[:12]}"
            airflow_dag.install_deps()
            airflow_dag.build_step(run_id)
            airflow_dag.convention_step(run_id)
            airflow_dag.mark_built()
        when = self.night + timedelta(hours=1)
        for task in ("deps", "build", "convention", "mark_built"):
            self.airflow.record("dbt_build", task, run_id, "success", run_after=when)
        return release.name

    def sync(self, connector: str, objects: list[str]) -> list[dict]:
        """One scheduled run of the sync DAG's mapped tasks; one outcome per object.

        A failed task is reported, not raised, as Airflow records it and the run goes on.
        """
        from vecten_platform.sync.airflow_dag import sync_one

        from .airflow import failure_log

        dag_id = f"sync_{connector}_default"
        run_id, when = self._next_night()
        outcomes = []
        with self.platform_runtime():
            for index, name in enumerate(objects):
                attempt = {"run_after": when, "map_index": index, "rendered": name}
                try:
                    rows = sync_one(connector, name)["rows"]
                except RuntimeError as exc:
                    outcomes.append({"object": name, "ok": False, "rows": 0, "error": str(exc)})
                    log = failure_log(type(exc).__name__, str(exc))
                    self.airflow.record(
                        dag_id, "sync", run_id, "failed", try_number=3, log=log, **attempt
                    )
                    continue
                outcomes.append({"object": name, "ok": True, "rows": rows, "error": None})
                self.airflow.record(dag_id, "sync", run_id, "success", **attempt)
        return outcomes

    def built(self) -> dict:
        marker = (self.artifacts / "dbt" / "built_commit").read_text()
        return json.loads(marker)

    @contextmanager
    def mcp(self, monkeypatch, served):
        """The MCP container restarted on the MCP host's current release."""
        release = self.release("mcp")
        environ = {
            **self._environ("mcp"),
            # The secrets volume outlives a release, so signing keys and sessions survive it.
            "VECTEN_MCP_SECRET_DIR": str(self.cloud_dir / "hosts" / "mcp" / "secrets"),
        }
        with served.serve(
            release, load_config(release), self.pg, monkeypatch, environ=environ
        ) as s:
            yield s
