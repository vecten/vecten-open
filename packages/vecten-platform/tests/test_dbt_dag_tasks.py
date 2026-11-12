"""The dbt DAG's task bodies, which run without Airflow."""

import json
import logging
import shutil
from pathlib import Path

import pytest

from vecten_platform.dbt import airflow_dag, runner
from vecten_platform.dbt.airflow_dag import (
    build_step,
    convention_step,
    dbt_assets,
    dbt_env_from_runtime,
    freshness_check,
    install_deps,
    mark_built,
    narrowed,
)
from vecten_platform.dbt.artifacts import (
    BUILD_FINISHED_KEY,
    BUILT_COMMIT_KEY,
    DirectorySink,
    run_keys,
)
from vecten_platform.dbt.runner import DbtRun
from vecten_platform.sync import context
from vecten_shared.config import VectenConfig, save_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec
from vecten_shared.versions import VERSIONS

BUILD_ARGS = ["build", "--exclude", "package:vecten_convention,test_type:singular"]
CONVENTION_ARGS = ["test", "--select", "package:vecten_convention,test_type:singular"]
RUN_ID = "manual__2026-10-17T00:00:00+00:00"


class FakeProvider:
    def __init__(self):
        self.asked: list[tuple[str, str]] = []

    def connection(self, env: str, name: str) -> ConnectionSpec:
        self.asked.append((env, name))
        return ConnectionSpec(
            kind="postgres",
            database="warehouse",
            role="loader",
            auth="password",
            host="postgres",
            port=5432,
        )

    def get_secret(self, name: str) -> str:
        assert name == "LOADER_PASSWORD"
        return "s3cret"


def write_config(root: Path, kind: str = "postgres") -> None:
    config = VectenConfig.model_validate(
        {
            "users": 1,
            "components": ["platform"],
            "project": {"name": "acme", "scaffold_version": VERSIONS.vecten},
            "warehouse": {"kind": kind},
        }
    )
    save_config(root, config)


def runtime_for(tmp_path: Path, monkeypatch, *, kind: str = "postgres", env: str = "prod") -> Path:
    root = tmp_path / "proj"
    (root / "dbt").mkdir(parents=True)
    write_config(root, kind)
    ctx = context.RuntimeContext(root, root / "connectors", env, FakeProvider(), tmp_path / "dlt")
    monkeypatch.setattr(context, "_RUNTIME", ctx)
    monkeypatch.setattr(airflow_dag, "SCRATCH_DIR", tmp_path / "scratch")
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = runtime_for(tmp_path, monkeypatch)
    # A stand-in for what the `deps` task installs, so the step tests exercise dbt only.
    (packages_dir(root) / "vecten_convention").mkdir(parents=True)
    monkeypatch.setenv("VECTEN_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("VECTEN_BUNDLE_COMMIT", raising=False)
    return root


def packages_dir(project: Path) -> Path:
    return project.parent / "scratch" / "dbt_packages"


def scratch(project: Path, name: str = "target") -> Path:
    path = project.parent / "scratch" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def sink(project: Path) -> DirectorySink:
    return DirectorySink(project.parent / "artifacts")


def fake_dbt(monkeypatch, outputs: dict[str, tuple[int, dict | None]]):
    """Stand in for `run_dbt`: per dbt command, its return code and the JSON it leaves behind."""
    calls: list[list[str]] = []

    def run(args, *, project_dir, env, capture=True):
        calls.append(list(args))
        code, doc = outputs[args[0]]
        if doc is not None:
            name = "sources.json" if args[:2] == ["source", "freshness"] else "run_results.json"
            target = Path(env["DBT_TARGET_PATH"])
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_text(json.dumps(doc))
        return DbtRun(code, f"dbt {args[0]} progress", "dbt said no" if code else "")

    monkeypatch.setattr(airflow_dag, "run_dbt", run)
    return calls


def results(*nodes: tuple[str, str, str | None]) -> dict:
    return {
        "results": [
            {"unique_id": uid, "status": status, "message": message}
            for uid, status, message in nodes
        ]
    }


def test_the_dag_steps_use_the_selectors_of_the_local_build(project, monkeypatch):
    calls = fake_dbt(monkeypatch, {"build": (0, results()), "test": (0, results())})
    build_step(RUN_ID)
    convention_step(RUN_ID)
    assert calls == [BUILD_ARGS, CONVENTION_ARGS]

    local: list[list[str]] = []

    def record(args, *, project_dir, env, capture=True):
        local.append(list(args))
        return DbtRun(0, "", "")

    monkeypatch.setattr(runner, "run_dbt", record)
    runner.run_build(project_dir=project / "dbt", env={}, in_process=False)
    assert local == calls


def test_build_archives_results_and_raises_naming_the_failed_node(project, monkeypatch):
    doc = results(
        ("model.acme.company", "success", None),
        ("test.acme.not_null_company_id.abc", "fail", "Got 2 results, configured to fail if != 0"),
    )
    fake_dbt(monkeypatch, {"build": (1, doc)})
    with pytest.raises(RuntimeError) as exc:
        build_step(RUN_ID)
    assert "not_null_company_id" in str(exc.value)
    archived = sink(project).get_text(run_keys(RUN_ID)["build"])
    assert json.loads(archived) == doc


def test_build_archives_even_when_dbt_cannot_start(project, monkeypatch):
    (scratch(project) / "run_results.json").write_text('{"results": []}')

    def boom(args, *, project_dir, env, capture=True):
        raise OSError("no interpreter")

    monkeypatch.setattr(airflow_dag, "run_dbt", boom)
    with pytest.raises(OSError):
        build_step(RUN_ID)
    # The earlier file is gone before dbt starts, so no other run's results are archived.
    assert sink(project).get_text(run_keys(RUN_ID)["build"]) is None


def test_a_failed_archive_names_the_artifact_directory(project, monkeypatch):
    class Unwritable:
        def put_file(self, key, path):
            raise PermissionError("denied")

    monkeypatch.setattr(airflow_dag, "sink_from_environ", lambda environ: Unwritable())
    fake_dbt(monkeypatch, {"build": (0, results())})
    with pytest.raises(RuntimeError) as exc:
        build_step(RUN_ID)
    artifacts = project.parent / "artifacts"
    assert str(exc.value) == f"results not archived to {artifacts}: denied"
    assert isinstance(exc.value.__cause__, PermissionError)


def test_a_build_that_writes_no_results_archives_nothing_and_still_fails(project, monkeypatch):
    (scratch(project) / "run_results.json").write_text('{"results": []}')
    fake_dbt(monkeypatch, {"build": (2, None)})
    with pytest.raises(RuntimeError, match="dbt said no"):
        build_step(RUN_ID)
    assert sink(project).get_text(run_keys(RUN_ID)["build"]) is None


def test_convention_archives_under_its_own_key_and_raises(project, monkeypatch):
    doc = results(("test.vecten_convention.marts_have_keys", "fail", "2 marts lack a key"))
    fake_dbt(monkeypatch, {"test": (1, doc)})
    with pytest.raises(RuntimeError, match="marts_have_keys"):
        convention_step(RUN_ID)
    assert json.loads(sink(project).get_text(run_keys(RUN_ID)["convention"])) == doc
    assert sink(project).get_text(run_keys(RUN_ID)["build"]) is None


def test_freshness_archives_sources_and_passes_on_warnings(project, monkeypatch):
    doc = results(("source.acme.demo.company", "warn", None))
    calls = fake_dbt(monkeypatch, {"source": (0, doc)})
    freshness_check(RUN_ID)
    assert calls == [["source", "freshness"]]
    assert json.loads(sink(project).get_text(run_keys(RUN_ID)["sources"])) == doc


def test_stale_sources_are_logged_and_the_task_succeeds(project, monkeypatch, caplog):
    doc = results(
        ("source.acme.demo.company", "error", None),
        ("source.acme.demo.deal", "pass", None),
        ("source.acme.demo.contact", "warn", None),
    )
    fake_dbt(monkeypatch, {"source": (1, doc)})
    with caplog.at_level(logging.WARNING, logger=airflow_dag.__name__):
        freshness_check(RUN_ID)
    stale = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stale source")]
    assert stale == ["stale source demo.company: error", "stale source demo.contact: warn"]
    assert all(r.levelno == logging.WARNING for r in caplog.records if r.getMessage() in stale)
    assert json.loads(sink(project).get_text(run_keys(RUN_ID)["sources"])) == doc


def test_freshness_fails_only_without_a_sources_record(project, monkeypatch):
    (scratch(project, "freshness") / "sources.json").write_text('{"results": []}')
    fake_dbt(monkeypatch, {"source": (2, None)})
    with pytest.raises(RuntimeError, match="(?s)dbt source freshness failed: .*dbt said no"):
        freshness_check(RUN_ID)
    assert sink(project).get_text(run_keys(RUN_ID)["sources"]) is None


def test_freshness_writes_to_its_own_target(project, monkeypatch):
    seen = {}

    def run(args, *, project_dir, env, capture=True):
        seen[args[0]] = env["DBT_TARGET_PATH"]
        target = Path(env["DBT_TARGET_PATH"])
        target.mkdir(parents=True, exist_ok=True)
        name = "sources.json" if args[0] == "source" else "run_results.json"
        (target / name).write_text('{"results": []}')
        return DbtRun(0, "", "")

    monkeypatch.setattr(airflow_dag, "run_dbt", run)
    build_step(RUN_ID)
    freshness_check(RUN_ID)
    scratch_dir = project.parent / "scratch"
    assert seen == {"build": str(scratch_dir / "target"), "source": str(scratch_dir / "freshness")}


def test_each_step_logs_dbt_output(project, monkeypatch, caplog):
    fake_dbt(monkeypatch, {"build": (1, results())})
    with caplog.at_level(logging.INFO, logger=airflow_dag.__name__):
        with pytest.raises(RuntimeError):
            build_step(RUN_ID)
    logged = {(r.levelno, r.getMessage()) for r in caplog.records}
    assert (logging.INFO, "dbt build output:\ndbt build progress") in logged
    assert (logging.WARNING, "dbt build stderr:\ndbt said no") in logged


@pytest.mark.parametrize("code", [0, 1])
def test_every_build_records_when_it_finished_beside_the_built_commit(project, monkeypatch, code):
    fake_dbt(monkeypatch, {"build": (code, results())})
    times = iter(["2026-11-12T10:00:00.000001+00:00", "2026-11-12T10:05:00.000002+00:00"])
    monkeypatch.setattr(airflow_dag, "_finished_at", lambda: next(times))
    for select in ("", "tag:intraday"):
        try:
            build_step(RUN_ID, select)
        except RuntimeError:
            assert code
        finished = sink(project).get_text(BUILD_FINISHED_KEY)
        assert BUILD_FINISHED_KEY.rsplit("/", 1)[0] == BUILT_COMMIT_KEY.rsplit("/", 1)[0]
        # A narrowed or failed build has still replaced relations, so each one records a new time.
        assert finished == (
            "2026-11-12T10:00:00.000001+00:00\n"
            if not select
            else "2026-11-12T10:05:00.000002+00:00\n"
        )
    assert sink(project).get_text(BUILT_COMMIT_KEY) is None


def test_an_unwritable_finish_time_leaves_the_build_result_alone(project, monkeypatch, caplog):
    fake_dbt(monkeypatch, {"build": (0, results())})

    class Refusing(DirectorySink):
        def put_text(self, key, text):
            raise OSError("read-only file system")

    monkeypatch.setattr(airflow_dag, "sink_from_environ", lambda environ: Refusing(Path("/")))
    monkeypatch.setattr(airflow_dag, "_archive", lambda key, path: None)
    with caplog.at_level(logging.WARNING):
        build_step(RUN_ID)
    assert "build finish time not recorded" in caplog.text


def test_mark_built_writes_the_bundle_commit(project, monkeypatch):
    monkeypatch.setenv("VECTEN_BUNDLE_COMMIT", "0d21a34")
    mark_built()
    assert sink(project).get_text(BUILT_COMMIT_KEY) == "0d21a34\n"


def test_mark_built_locally_records_the_semantic_content_revision(project):
    from vecten_shared.osi.revision import content_revision

    mark_built()
    assert sink(project).get_text(BUILT_COMMIT_KEY) == content_revision(project / "osi") + "\n"


def test_mark_built_records_shipped_ancestry_only_for_the_current_bundle(project, monkeypatch):
    monkeypatch.setenv("VECTEN_BUNDLE_COMMIT", "b" * 40)
    lineage = project / "lineage.json"
    marker = {"commit": "b" * 40, "ancestors": ["a" * 40]}
    lineage.write_text(json.dumps(marker))
    monkeypatch.setenv("VECTEN_BUNDLE_LINEAGE", str(lineage))
    mark_built()
    assert json.loads(sink(project).get_text(BUILT_COMMIT_KEY)) == marker
    lineage.write_text(json.dumps({"commit": "c" * 40, "ancestors": ["a" * 40]}))
    with pytest.raises(ValueError, match="lineage"):
        mark_built()


def test_dbt_env_reads_the_warehouse_connection_and_the_loader_password(project):
    ctx = context.runtime()
    env = dbt_env_from_runtime(ctx)
    assert ctx.provider.asked == [("prod", "warehouse")]
    assert env["DBT_PROJECT_DIR"] == str(project / "dbt")
    assert env["DBT_PROFILES_DIR"] == str(project / "dbt")
    assert env["WAREHOUSE_HOST"] == "postgres" and env["WAREHOUSE_PORT"] == "5432"
    assert env["WAREHOUSE_USER"] == "loader" and env["WAREHOUSE_PASSWORD"] == "s3cret"
    assert env["DBT_TARGET"] == "local"


def test_dbt_env_keeps_build_output_out_of_the_project(project):
    env = dbt_env_from_runtime(context.runtime())
    scratch_dir = project.parent / "scratch"
    assert env["DBT_TARGET_PATH"] == str(scratch_dir / "target")
    assert env["DBT_PACKAGES_INSTALL_PATH"] == str(scratch_dir / "dbt_packages")
    empty = Path(env[runner.OSI_PATH_VAR])
    assert empty.is_dir() and not any(empty.iterdir())


def test_dbt_env_refuses_a_connection_of_another_kind(tmp_path, monkeypatch):
    runtime_for(tmp_path, monkeypatch, kind="snowflake", env="local")
    with pytest.raises(RuntimeError) as exc:
        dbt_env_from_runtime(context.runtime())
    assert str(exc.value) == (
        "the local stack offers a postgres connection but the project's warehouse is snowflake; "
        "the dbt DAG runs after deploy"
    )


def test_install_deps_resolves_as_a_subprocess_against_the_project(project, monkeypatch, caplog):
    seen = {}

    def resolve(*, project_dir, config, env, in_process):
        seen.update(project_dir=project_dir, config=config, in_process=in_process, env=env)
        return DbtRun(0, "Installed vecten_convention", "")

    monkeypatch.setattr(airflow_dag, "resolve_dependencies", resolve)
    with caplog.at_level(logging.INFO, logger=airflow_dag.__name__):
        install_deps()
    assert seen["project_dir"] == project / "dbt"
    assert seen["config"].project.name == "acme"
    assert seen["in_process"] is False
    assert seen["env"]["WAREHOUSE_PASSWORD"] == "s3cret"
    assert seen["env"]["DBT_PACKAGES_INSTALL_PATH"] == str(
        project.parent / "scratch" / "dbt_packages"
    )
    assert "Installed vecten_convention" in caplog.text


def record_steps(monkeypatch, *, dbt_code=0, installs=True, deps_error=None) -> list[str]:
    """Stand in for dependency resolution and dbt, recording the order they ran in."""
    events: list[str] = []

    def resolve(*, project_dir, config, env, in_process):
        events.append("deps")
        if deps_error:
            raise Failure(ExitCode.TOOL_FAILED, deps_error)
        if installs:
            (Path(env["DBT_PACKAGES_INSTALL_PATH"]) / "vecten_convention").mkdir(parents=True)
        return DbtRun(0, "Installed vecten_convention", "")

    def run(args, *, project_dir, env, capture=True):
        events.append(args[0])
        target = Path(env["DBT_TARGET_PATH"])
        target.mkdir(parents=True, exist_ok=True)
        name = "sources.json" if args[0] == "source" else "run_results.json"
        (target / name).write_text('{"results": []}')
        return DbtRun(dbt_code, "", "dbt said no" if dbt_code else "")

    monkeypatch.setattr(airflow_dag, "resolve_dependencies", resolve)
    monkeypatch.setattr(airflow_dag, "run_dbt", run)
    return events


@pytest.mark.parametrize(
    ("step", "command"),
    [(build_step, "build"), (convention_step, "test"), (freshness_check, "source")],
)
def test_a_step_on_an_empty_scratch_dir_resolves_once_then_runs_dbt(
    project, monkeypatch, step, command
):
    shutil.rmtree(packages_dir(project))
    events = record_steps(monkeypatch)
    step(RUN_ID)
    assert events == ["deps", command]


def test_a_step_after_install_deps_does_not_resolve_again(project, monkeypatch):
    shutil.rmtree(packages_dir(project))
    events = record_steps(monkeypatch)
    install_deps()
    build_step(RUN_ID)
    assert events == ["deps", "build"]


def test_a_failed_resolution_fails_the_step_without_calling_dbt(project, monkeypatch):
    shutil.rmtree(packages_dir(project))
    events = record_steps(monkeypatch, deps_error="dbt deps failed: no network")
    with pytest.raises(Failure, match="dbt deps failed: no network"):
        build_step(RUN_ID)
    assert events == ["deps"]
    assert sink(project).get_text(run_keys(RUN_ID)["build"]) is None


def test_a_resolution_that_installs_no_convention_package_runs_dbt_once(project, monkeypatch):
    shutil.rmtree(packages_dir(project))
    events = record_steps(monkeypatch, dbt_code=1, installs=False)
    with pytest.raises(RuntimeError, match="dbt said no"):
        build_step(RUN_ID)
    assert events == ["deps", "build"]


def test_assets_cover_every_connector_group_in_order():
    cfg = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "a", "scaffold_version": "0.1.0"},
            "connectors": [
                {"name": "stripe", "source": "dlt"},
                {"name": "hubspot", "source": "dlt", "groups": {"default": [], "crm": ["deals"]}},
            ],
        }
    )
    assert dbt_assets(cfg) == [("hubspot", "crm"), ("hubspot", "default"), ("stripe", "default")]


def grouped_config() -> VectenConfig:
    return VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "a", "scaffold_version": "0.1.0"},
            "connectors": [
                {"name": "stripe", "source": "dlt", "groups": {"daily": [], "hourly": []}},
                {"name": "hubspot", "source": "dlt", "groups": {"daily": [], "weekly": []}},
            ],
        }
    )


def test_assets_narrow_to_the_named_groups():
    cfg = grouped_config()
    assert dbt_assets(cfg, ["daily"]) == [("hubspot", "daily"), ("stripe", "daily")]
    assert dbt_assets(cfg, ["stripe.hourly", "hubspot.weekly"]) == [
        ("hubspot", "weekly"),
        ("stripe", "hourly"),
    ]
    assert dbt_assets(cfg, "daily") == [("hubspot", "daily"), ("stripe", "daily")]
    assert dbt_assets(cfg, []) == []


def test_a_group_no_connector_has_is_refused():
    with pytest.raises(ValueError, match="no connector has the group dialy, stripe.weekly"):
        dbt_assets(grouped_config(), ["dialy", "stripe.weekly", "daily"])


def test_build_passes_the_run_parameters_to_dbt(project, monkeypatch):
    calls = fake_dbt(monkeypatch, {"build": (0, results())})
    build_step(RUN_ID, "tag:intraday", "marts", full_refresh=True)
    assert calls == [
        [
            "build",
            "--select",
            "tag:intraday",
            "--exclude",
            "package:vecten_convention,test_type:singular",
            "marts",
            "--full-refresh",
        ]
    ]


def test_only_a_selection_or_exclusion_narrows_a_build():
    assert not narrowed("", "") and not narrowed((), [])
    assert narrowed("core", "") and narrowed("", ["marts"])


class SnowflakeProvider:
    """The cloud provider of a Snowflake project: the analysts' connection, the loader's key."""

    def __init__(self):
        self.read: list[str] = []

    def connection(self, env: str, name: str) -> ConnectionSpec:
        return ConnectionSpec(
            kind="snowflake",
            database="ACME",
            role="ANALYST",
            auth="external_oauth",
            account="ACME-PROD",
            warehouse="ACME_PROD_WH",
        )

    def get_secret(self, name: str) -> str:
        self.read.append(name)
        return "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"


def test_dbt_env_on_snowflake_connects_as_the_loader_by_key_pair(tmp_path, monkeypatch):
    root = runtime_for(tmp_path, monkeypatch, kind="snowflake", env="prod")
    provider = SnowflakeProvider()
    ctx = context.RuntimeContext(root, root / "connectors", "prod", provider, tmp_path / "dlt")
    monkeypatch.setattr(context, "_RUNTIME", ctx)
    env = dbt_env_from_runtime(context.runtime())
    assert provider.read == ["SNOWFLAKE_LOADER_PRIVATE_KEY"]
    assert env["SNOWFLAKE_USER"] == "LOADER" and env["SNOWFLAKE_ROLE"] == "LOADER"
    assert env["SNOWFLAKE_PRIVATE_KEY"].startswith("-----BEGIN PRIVATE KEY-----")
    assert "SNOWFLAKE_PASSWORD" not in env
    assert env["SNOWFLAKE_DATABASE"] == "ACME" and env["SNOWFLAKE_WAREHOUSE"] == "ACME_PROD_WH"
