import hashlib
import json
import subprocess
import sys
import tomllib

import pytest
import yaml

from vecten.connectors import extensions as ext_mod
from vecten.connectors.verified import SourceFiles
from vecten_shared.output import ExitCode, Failure

LOCAL = ("--name", "acme", "--users", "3", "--source", "hubspot", "--source", "stripe")


class FakeVerified:
    names_seen = {"hubspot", "google_sheets"}

    def names(self):
        return self.names_seen

    def materialize(self, name, target):
        target.mkdir(parents=True)
        (target / "__init__.py").write_text(
            f"import dlt\n\n@dlt.source(name={name!r})\n"
            f"def {name}(api_key: str = dlt.secrets.value, refresh: bool = False):\n    return []\n"
        )
        (target / "helpers.py").write_text("X = 1\n")
        reqs = ["google-api-python-client>=2,<3"] if name == "google_sheets" else []
        return SourceFiles(requirements=reqs)


class FakeEntryPoint:
    def __init__(self, name, dist_name, obj):
        self.name, self.obj = name, obj
        self.dist = type("Dist", (), {"name": dist_name, "version": "2.3.0"})()

    def load(self):
        return self.obj


def attio_connector():
    def write(target):
        (target / "connector.py").write_text("def source():\n    return None\n")

    return {
        "kind": "vecten-managed",
        "secrets": ["ATTIO_API_KEY"],
        "dependencies": ["attio-sdk>=1,<2"],
        "write": write,
    }


@pytest.fixture
def project(invoke, workdir, monkeypatch):
    monkeypatch.setenv("VECTEN_VERIFIED_SOURCES", f"{__name__}:FakeVerified")
    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    monkeypatch.chdir(workdir / "p")
    return workdir / "p"


def config(project):
    return tomllib.loads((project / "vecten.toml").read_text())


def test_add_verified_source(invoke, project):
    r = invoke("connector", "add", "hubspot", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["source"] == "dlt" and doc["secrets"] == ["HUBSPOT_API_KEY"]
    assert (project / "connectors" / "hubspot" / "source" / "__init__.py").is_file()
    assert (project / "connectors" / "hubspot" / "source" / "helpers.py").is_file()
    connector_py = (project / "connectors" / "hubspot" / "connector.py").read_text()
    assert "from .source import hubspot" in connector_py
    assert 'api_key=secret("HUBSPOT_API_KEY")' in connector_py
    assert (project / "connectors" / "hubspot" / "__init__.py").is_file()
    assert (project / "connectors" / "hubspot" / "fixtures" / ".gitkeep").is_file()
    test_py = (project / "connectors" / "hubspot" / "test_hubspot.py").read_text()
    assert "replay_connector_test" in test_py and "vecten connector run hubspot --record" in test_py
    flat = " ".join(test_py.split())
    assert "never runs connector.py" in flat
    assert "`vecten connector run hubspot --json` is the live proof" in flat
    dag = (project / "orchestration" / "dags" / "sync_hubspot_default.py").read_text()
    assert dag.endswith(
        'build_sync_dag(connector="hubspot", group="default", objects=[], schedule="@daily")\n'
    )
    sources = yaml.safe_load((project / "dbt" / "models" / "staging" / "sources.yml").read_text())
    entry = sources["sources"][0]
    assert (entry["name"], entry["schema"], entry["tables"]) == ("hubspot", "raw_hubspot", [])
    assert entry["config"]["freshness"]["error_after"] == {"count": 72, "period": "hour"}
    cfg = config(project)
    decl = cfg["connectors"][0]
    assert decl["name"] == "hubspot" and decl["source"] == "dlt"
    assert decl["secrets"] == ["HUBSPOT_API_KEY"] and decl["groups"] == {"default": []}
    assert decl["scaffold_hash"] == hashlib.sha256(connector_py.encode()).hexdigest()
    assert cfg["wanted"] == ["stripe"]


def test_add_records_requirements_as_dependencies(invoke, project):
    r = invoke("connector", "add", "google_sheets", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    doc = json.loads(r.stdout)
    assert doc["dependencies"] == ["google-api-python-client>=2,<3"]
    assert "google-api-python-client" in r.stdout
    assert config(project)["connectors"][0]["dependencies"] == ["google-api-python-client>=2,<3"]


def test_add_extension_connector(invoke, project, monkeypatch):
    monkeypatch.setattr(
        ext_mod,
        "entry_points_for",
        lambda group: [FakeEntryPoint("attio", "acme-ext", attio_connector)],
    )
    cfg = (
        (project / "vecten.toml")
        .read_text()
        .replace("extensions = []", 'extensions = ["acme-ext"]')
    )
    (project / "vecten.toml").write_text(cfg)
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert (project / "connectors" / "attio" / "connector.py").is_file()
    decl = config(project)["connectors"][0]
    assert (
        decl["source"] == "vecten-managed"
        and decl["extension"] == "acme-ext"
        and decl["version"] == "2.3.0"
    )
    assert decl["secrets"] == ["ATTIO_API_KEY"] and decl["dependencies"] == ["attio-sdk>=1,<2"]


def grouped_connector():
    spec = attio_connector()
    spec["groups"] = {
        "daily": {"objects": ["companies", "people"], "schedule": "0 6 * * *"},
        "hourly": {"objects": ["deals"], "schedule": "@hourly", "timezone": "Europe/Warsaw"},
        "weekly": ["notes"],
    }
    return spec


def use_extension(project, monkeypatch, factory):
    monkeypatch.setattr(
        ext_mod, "entry_points_for", lambda group: [FakeEntryPoint("attio", "acme-ext", factory)]
    )
    list_extension(project)


def dag_text(project, name):
    return (project / "orchestration" / "dags" / f"sync_{name}.py").read_text()


def test_extension_groups_get_a_dag_each_on_their_own_schedule(invoke, project, monkeypatch):
    use_extension(project, monkeypatch, grouped_connector)
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert sorted(json.loads(r.stdout)["dags"]) == [
        "orchestration/dags/sync_attio_daily.py",
        "orchestration/dags/sync_attio_hourly.py",
        "orchestration/dags/sync_attio_weekly.py",
    ]
    assert config(project)["connectors"][0]["groups"] == {
        "daily": ["companies", "people"],
        "hourly": ["deals"],
        "weekly": ["notes"],
    }
    daily, hourly, weekly = (dag_text(project, f"attio_{g}") for g in ("daily", "hourly", "weekly"))
    assert 'objects=["companies", "people"], schedule="0 6 * * *")' in daily
    assert 'objects=["deals"], schedule="@hourly", timezone="Europe/Warsaw")' in hourly
    assert 'objects=["notes"], schedule="@daily")' in weekly
    for text in (daily, hourly, weekly):
        compile(text, "dag.py", "exec")


def test_a_declared_extension_connector_keeps_its_group_schedules(invoke, project, monkeypatch):
    use_extension(project, monkeypatch, grouped_connector)
    _declare(
        project,
        "attio",
        "vecten-managed",
        secrets=["ATTIO_API_KEY"],
        extension="acme-ext",
        groups={"hourly": ["deals"], "extra": []},
    )
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert 'schedule="@hourly", timezone="Europe/Warsaw")' in dag_text(project, "attio_hourly")
    assert dag_text(project, "attio_extra").endswith('objects=[], schedule="@daily")\n')
    assert not (project / "orchestration" / "dags" / "sync_attio_daily.py").exists()


def test_an_extension_without_groups_gets_the_default_group(invoke, project, monkeypatch):
    use_extension(project, monkeypatch, attio_connector)
    assert invoke("connector", "add", "attio", "--json").exit_code == 0
    assert config(project)["connectors"][0]["groups"] == {"default": []}
    assert dag_text(project, "attio_default").endswith('objects=[], schedule="@daily")\n')


def _with_groups(groups):
    def factory():
        spec = attio_connector()
        spec["groups"] = groups
        return spec

    return factory


@pytest.mark.parametrize(
    ("groups", "detail"),
    [
        (["daily"], "not a dict of groups"),
        ({}, "not a dict of groups"),
        ({"Daily!": []}, "group names must be"),
        ({"daily": "companies"}, "neither a dict nor a list"),
        ({"daily": {"objects": [], "cron": "@daily"}}, "unknown keys ['cron']"),
        ({"daily": {"objects": "companies"}}, "objects that is not a list of strings"),
        ({"daily": {"schedule": 6}}, "schedule is not a cron string"),
        ({"daily": {"schedule": '@daily")\nimport os'}}, "schedule is not a cron string"),
        ({"daily": {"timezone": "Mars/Olympus"}}, "unknown timezone 'Mars/Olympus'"),
    ],
)
def test_extension_groups_that_cannot_render_a_dag_are_config(
    invoke, project, monkeypatch, groups, detail
):
    use_extension(project, monkeypatch, _with_groups(groups))
    before = snapshot(project)
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 6, r.stdout
    err = json.loads(r.stdout)["error"]
    assert "acme-ext" in err and "attio" in err and detail in err
    assert snapshot(project) == before


def test_extension_not_listed_in_config_is_ignored(invoke, project, monkeypatch):
    monkeypatch.setattr(
        ext_mod,
        "entry_points_for",
        lambda group: [FakeEntryPoint("attio", "acme-ext", attio_connector)],
    )
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 4
    assert "neither a dlt verified source nor" in json.loads(r.stdout)["error"]


def test_unknown_source_is_refused_and_renders_nothing(invoke, project):
    before = sorted(str(p) for p in project.rglob("*"))
    r = invoke("connector", "add", "nosuchsource", "--json")
    assert r.exit_code == 4
    err = json.loads(r.stdout)["error"]
    assert "--from rest|sql|filesystem" in err and "connector skill" in err
    assert sorted(str(p) for p in project.rglob("*")) == before
    assert config(project)["connectors"] == []


@pytest.mark.parametrize(
    "kind,marker",
    [("rest", "rest_api_source"), ("sql", "sql_database"), ("filesystem", "filesystem")],
)
def test_generic_scaffolds(invoke, project, kind, marker):
    r = invoke("connector", "add", "billing", "--from", kind, "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    text = (project / "connectors" / "billing" / "connector.py").read_text()
    assert marker in text and "TODO" in text
    assert not (project / "connectors" / "billing" / "source").exists()
    decl = config(project)["connectors"][0]
    assert decl["source"] == kind
    assert (
        decl["secrets"]
        == {
            "rest": ["BILLING_API_KEY"],
            "sql": ["BILLING_DATABASE_URL"],
            "filesystem": ["BILLING_BUCKET_URL"],
        }[kind]
    )
    for secret in decl["secrets"]:
        assert f'secret("{secret}")' in text


@pytest.mark.parametrize("kind", ["rest", "sql", "filesystem"])
def test_generic_scaffold_passes_the_project_ruff_check(invoke, project, kind):
    # The project's ci workflow runs `ruff check .` with no config of its own, so
    # vecten_platform sorts as a third-party import there.
    assert invoke("connector", "add", "billing", "--from", kind, "--json").exit_code == 0
    r = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", str(project)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_rest_scaffold_names_the_placeholders(invoke, project):
    invoke("connector", "add", "billing", "--from", "rest", "--json")
    text = (project / "connectors" / "billing" / "connector.py").read_text()
    for word in ("base_url", "resources", "paginator", "incremental"):
        assert word in text


def test_unknown_generic_kind_lists_the_three(invoke, project):
    r = invoke("connector", "add", "billing", "--from", "kafka", "--json")
    assert r.exit_code == 2
    assert "rest, sql, filesystem" in json.loads(r.stdout)["error"]
    assert not (project / "connectors" / "billing").exists()


def test_add_refuses_bad_names(invoke, project):
    r = invoke("connector", "add", "Hub-Spot", "--json")
    assert r.exit_code == 2
    assert "lowercase" in json.loads(r.stdout)["error"]
    assert not (project / "connectors" / "Hub-Spot").exists()
    assert config(project)["connectors"] == []


def test_add_twice_is_refused(invoke, project):
    assert invoke("connector", "add", "hubspot", "--json").exit_code == 0
    r = invoke("connector", "add", "hubspot", "--json")
    assert r.exit_code == 4 and "already exists" in json.loads(r.stdout)["error"]
    assert len(config(project)["connectors"]) == 1


def _declare(project, name, source, **extra):
    from vecten_shared.config import ConnectorDecl, load_config, save_config

    cfg = load_config(project)
    cfg.connectors.append(ConnectorDecl(name=name, source=source, **extra))
    save_config(project, cfg)


def test_declared_dlt_connector_without_directory_is_rerendered(invoke, project):
    _declare(
        project,
        "hubspot",
        "dlt",
        secrets=["HUBSPOT_API_KEY"],
        access={"groups": ["sales"]},
        groups={"default": [], "nightly": ["companies"]},
    )
    dag = project / "orchestration" / "dags" / "sync_hubspot_default.py"
    dag.parent.mkdir(parents=True, exist_ok=True)
    dag.write_text("# mine\n")
    before = config(project)["connectors"][0]
    r = invoke("connector", "add", "hubspot", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    connector_py = project / "connectors" / "hubspot" / "connector.py"
    assert "from .source import hubspot" in connector_py.read_text()
    assert (project / "connectors" / "hubspot" / "source" / "__init__.py").is_file()
    assert dag.read_text() == "# mine\n"
    assert (project / "orchestration" / "dags" / "sync_hubspot_nightly.py").is_file()
    cfg = config(project)
    assert len(cfg["connectors"]) == 1
    after = dict(cfg["connectors"][0])
    assert after.pop("scaffold_hash") == hashlib.sha256(connector_py.read_bytes()).hexdigest()
    assert after == before
    assert cfg["wanted"] == ["hubspot", "stripe"]
    sources = yaml.safe_load((project / "dbt" / "models" / "staging" / "sources.yml").read_text())
    assert [s["name"] for s in sources["sources"]] == ["hubspot"]


def test_declared_rest_connector_without_directory_gets_the_scaffold(invoke, project):
    _declare(project, "billing", "rest", secrets=["BILLING_API_KEY"])
    r = invoke("connector", "add", "billing", "--from", "sql", "--json")
    assert r.exit_code == 2, r.stdout
    assert "rest" in json.loads(r.stdout)["error"]
    assert not (project / "connectors" / "billing").exists()
    r = invoke("connector", "add", "billing", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    text = (project / "connectors" / "billing" / "connector.py").read_text()
    assert "rest_api_source" in text and 'secret("BILLING_API_KEY")' in text
    assert config(project)["connectors"][0]["source"] == "rest"


def test_declared_custom_connector_without_directory_is_refused(invoke, project):
    _declare(project, "billing", "custom", secrets=["BILLING_API_KEY"])
    r = invoke("connector", "add", "billing", "--json")
    assert r.exit_code == 4, r.stdout
    err = json.loads(r.stdout)["error"]
    assert "version control" in err and "connector skill" in err
    assert not (project / "connectors" / "billing").exists()


def test_declared_extension_connector_without_directory_is_rerendered(invoke, project, monkeypatch):
    monkeypatch.setattr(
        ext_mod,
        "entry_points_for",
        lambda group: [FakeEntryPoint("attio", "acme-ext", attio_connector)],
    )
    _declare(
        project,
        "attio",
        "vecten-managed",
        secrets=["ATTIO_API_KEY"],
        extension="acme-ext",
        version="2.0.0",
    )
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 4 and "acme-ext" in json.loads(r.stdout)["error"]
    list_extension(project)
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert (project / "connectors" / "attio" / "connector.py").is_file()
    assert config(project)["connectors"][0]["version"] == "2.0.0"


def test_declared_connector_with_a_directory_is_still_refused(invoke, project):
    _declare(project, "billing", "rest", secrets=["BILLING_API_KEY"])
    (project / "connectors" / "billing").mkdir(parents=True)
    r = invoke("connector", "add", "billing", "--json")
    assert r.exit_code == 4 and "already exists" in json.loads(r.stdout)["error"]


def test_add_outside_platform_component_is_refused(invoke, workdir, monkeypatch):
    monkeypatch.setenv("VECTEN_VERIFIED_SOURCES", f"{__name__}:FakeVerified")
    assert (
        invoke(
            "init",
            "m",
            "--name",
            "acme",
            "--users",
            "3",
            "--has-warehouse",
            "yes",
            "--warehouse-url",
            "postgres://h/dw",
            "--json",
        ).exit_code
        == 0
    )
    monkeypatch.chdir(workdir / "m")
    r = invoke("connector", "add", "hubspot", "--json")
    assert r.exit_code == 4 and "platform" in json.loads(r.stdout)["error"]


def test_add_outside_a_project_is_refused(invoke):
    r = invoke("connector", "add", "hubspot", "--json")
    assert r.exit_code == 4


def test_rendered_connector_imports_and_test_file_skips(invoke, project, tmp_path):
    import subprocess
    import sys

    assert invoke("connector", "add", "hubspot", "--json").exit_code == 0
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "connectors/hubspot/test_hubspot.py",
            "-o",
            "filterwarnings=ignore",
        ],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 skipped" in proc.stdout


def snapshot(project):
    return sorted(str(p) for p in project.rglob("*")), (project / "vecten.toml").read_text()


def list_extension(project):
    cfg = (project / "vecten.toml").read_text()
    (project / "vecten.toml").write_text(
        cfg.replace("extensions = []", 'extensions = ["acme-ext"]')
    )


def test_add_without_a_name_is_usage_as_json(invoke, project):
    r = invoke("connector", "add", "--json")
    assert r.exit_code == 2
    doc = json.loads(r.stdout)
    assert doc["code_name"] == "USAGE" and "source name" in doc["error"]


@pytest.mark.parametrize("text", ["sources: [unclosed\n", "- name: hubspot\n"])
def test_unreadable_sources_yml_refuses_before_writing(invoke, project, text):
    path = project / "dbt" / "models" / "staging" / "sources.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    before = snapshot(project)
    r = invoke("connector", "add", "hubspot", "--json")
    assert r.exit_code == 6, r.stdout
    assert "sources.yml" in json.loads(r.stdout)["error"]
    assert snapshot(project) == before


def test_existing_dag_file_is_refused_before_writing(invoke, project):
    dag = project / "orchestration" / "dags" / "sync_hubspot_default.py"
    dag.parent.mkdir(parents=True, exist_ok=True)
    dag.write_text("# mine\n")
    before = snapshot(project)
    r = invoke("connector", "add", "hubspot", "--json")
    assert r.exit_code == 4 and "sync_hubspot_default.py" in json.loads(r.stdout)["error"]
    assert snapshot(project) == before
    assert dag.read_text() == "# mine\n"


def test_extension_write_failure_is_tool_failed(invoke, project, monkeypatch):
    def broken():
        def write(target):
            raise RuntimeError("disk on fire")

        return {"kind": "custom", "write": write}

    monkeypatch.setattr(
        ext_mod, "entry_points_for", lambda group: [FakeEntryPoint("attio", "acme-ext", broken)]
    )
    list_extension(project)
    before = snapshot(project)
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 5
    err = json.loads(r.stdout)["error"]
    assert "acme-ext" in err and "RuntimeError: disk on fire" in err
    assert snapshot(project) == before


class RaisingEntryPoint(FakeEntryPoint):
    def load(self):
        raise ImportError("no module named acme_ext")


def _no_write():
    return {"kind": "custom"}


def _bad_secret():
    spec = attio_connector()
    spec["secrets"] = ["attio-key"]
    return spec


def _secrets_as_string():
    spec = attio_connector()
    spec["secrets"] = "ATTIO_API_KEY"
    return spec


def _dependencies_not_strings():
    spec = attio_connector()
    spec["dependencies"] = [{"name": "attio-sdk"}]
    return spec


@pytest.mark.parametrize(
    "ep",
    [
        RaisingEntryPoint("attio", "acme-ext", None),
        FakeEntryPoint("attio", "acme-ext", lambda: ["not", "a", "dict"]),
        FakeEntryPoint("attio", "acme-ext", _no_write),
        FakeEntryPoint("attio", "acme-ext", _bad_secret),
        FakeEntryPoint("attio", "acme-ext", _secrets_as_string),
        FakeEntryPoint("attio", "acme-ext", _dependencies_not_strings),
    ],
    ids=[
        "load-raises",
        "not-a-dict",
        "no-write",
        "bad-secret-name",
        "secrets-a-string",
        "dependencies-not-strings",
    ],
)
def test_broken_extension_spec_is_config(invoke, project, monkeypatch, ep):
    monkeypatch.setattr(ext_mod, "entry_points_for", lambda group: [ep])
    list_extension(project)
    r = invoke("connector", "add", "attio", "--json")
    assert r.exit_code == 6, r.stdout
    err = json.loads(r.stdout)["error"]
    assert "acme-ext" in err and "attio" in err


def _record_locks(monkeypatch):
    from vecten import extensions

    calls = []
    monkeypatch.setattr(extensions, "lock", lambda root, upgrade=(): calls.append(root) or {})
    return calls


def test_add_relocks_a_project_that_has_locks(invoke, project, monkeypatch):
    calls = _record_locks(monkeypatch)
    (project / "requirements.lock").write_text("# vecten-lock: component=cli inputs=x\n")
    r = invoke("connector", "add", "google_sheets", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert calls == [project]
    assert json.loads(r.stdout)["locked"] is True


def test_add_leaves_a_project_without_locks_unlocked(invoke, project, monkeypatch):
    calls = _record_locks(monkeypatch)
    r = invoke("connector", "add", "google_sheets", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert calls == []
    assert json.loads(r.stdout)["locked"] is False


def test_a_failed_relock_keeps_the_connector_and_names_vecten_lock(invoke, project, monkeypatch):
    from vecten import extensions

    def fail(root, upgrade=()):
        raise Failure(ExitCode.TOOL_FAILED, "requirements-airflow.lock: resolution failed")

    monkeypatch.setattr(extensions, "lock", fail)
    (project / "requirements.lock").write_text("# vecten-lock: component=cli inputs=x\n")
    r = invoke("connector", "add", "google_sheets", "--json")
    assert r.exit_code != 0
    assert "vecten lock" in r.stdout + r.stderr
    assert (project / "connectors" / "google_sheets" / "connector.py").is_file()


def test_add_without_dependencies_keeps_fresh_locks_without_relocking(invoke, project, monkeypatch):
    from vecten import locking

    calls = _record_locks(monkeypatch)
    (project / "requirements.lock").write_text("# vecten-lock: component=cli inputs=x\n")
    monkeypatch.setattr(locking, "lock_problems", lambda root, config: [])
    r = invoke("connector", "add", "crm", "--from", "rest", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert calls == []


def test_add_without_dependencies_never_relocks_a_legacy_project(invoke, project, monkeypatch):
    calls = _record_locks(monkeypatch)
    # Locks an earlier release wrote: unstamped, and no extensions.lock.json.
    (project / "requirements.lock").write_text("helper==1.0\n")
    r = invoke("connector", "add", "crm", "--from", "rest", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert calls == []


def test_a_connector_named_like_an_area_is_refused_before_anything_is_written(invoke, project):
    from vecten_shared.config import AreaDecl, load_config, save_config

    save_config(project, load_config(project).model_copy(update={"areas": {"hubspot": AreaDecl()}}))
    before = (project / "vecten.toml").read_text()
    result = invoke("connector", "add", "hubspot", "--json")
    assert result.exit_code == ExitCode.REFUSED, result.stdout
    assert "area hubspot" in result.stdout
    assert not (project / "connectors" / "hubspot").exists()
    assert (project / "vecten.toml").read_text() == before
