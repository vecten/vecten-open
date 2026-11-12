"""`vecten lock`: per-component locks, their recorded inputs, and the extension record."""

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomli_w

from vecten import extensions, locking
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import VERSIONS

from .test_in_repo import make


@pytest.fixture
def project(invoke, workdir, monkeypatch):
    assert (
        invoke(
            "init", "p", "--name", "demo", "--users", "2", "--source", "demo", "--json"
        ).exit_code
        == 0
    )
    root = workdir / "p"
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def fakes(monkeypatch):
    """uv, Docker and the CLI's environment replaced; every compile is recorded."""
    seen = {"compiles": [], "installed": [], "inspected": []}
    packages = {"acme-demo": ("1.2.0", ("cli", "mcp", "platform"))}

    def inspect(package, root):
        name = package.split("[")[0].split("=")[0].split(">")[0]
        seen["inspected"].append(package)
        version, extras = packages[name]
        return extensions.PackageMetadata(
            name=name,
            version=version,
            entry_points=(SimpleNamespace(group="vecten.dags"),),
            extras=extras,
        )

    def resolve(roots, root, *, constraints="", runtime=False, previous=None, upgrade=(), **kw):
        seen["compiles"].append(
            {"roots": roots, "runtime": runtime, "previous": previous, "upgrade": upgrade, **kw}
        )
        # uv echoes a path input as given, without its extras.
        paths = [r.split("[")[0] + "\n" for r in roots if r.startswith("./")]
        return "".join(paths) + "".join(f"{r}\n" for r in roots if "==" in r) + "helper==1.0\n"

    monkeypatch.setattr(extensions, "inspect_package", inspect)
    monkeypatch.setattr(extensions, "resolve_lock", resolve)
    monkeypatch.setattr(extensions, "base_constraints", lambda kind: f"base-{kind}==1.0\n")
    monkeypatch.setattr(
        extensions,
        "install_lock",
        lambda lock, root, editable=(): seen["installed"].append((lock, tuple(editable))),
    )
    seen["packages"] = packages
    return seen


def _edit_config(root, change):
    doc = tomllib.loads((root / "vecten.toml").read_text())
    change(doc)
    (root / "vecten.toml").write_text(tomli_w.dumps(doc))


def _add_extension(root):
    _edit_config(root, lambda doc: doc.__setitem__("extensions", ["acme-demo"]))


def test_lock_writes_every_record_with_inputs_lines(invoke, project, fakes):
    _add_extension(project)
    r = invoke("lock", "--json")
    assert r.exit_code == 0, r.output
    assert (project / "requirements.txt").read_text() == "acme-demo==1.2.0\n"
    for component, name in locking.LOCK_FILES.items():
        assert locking.read_stamp((project / name).read_text())[0] == component
    record = json.loads((project / locking.RECORD_FILE).read_text())
    assert record["extensions"]["acme-demo"] == {
        "version": "1.2.0",
        "extras": ["cli", "mcp", "platform"],
        "secrets": [],
        "settings": None,
    }
    assert locking.lock_problems(project, load_config(project)) == []
    assert "acme-demo[cli]==1.2.0" in fakes["installed"][0][0]


def test_each_lock_requests_its_components_extra(invoke, project, fakes):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    assert "acme-demo[platform]==1.2.0" in (project / "requirements-airflow.lock").read_text()
    assert "acme-demo[mcp]==1.2.0" in (project / "requirements-mcp.lock").read_text()
    cli = (project / "requirements.lock").read_text()
    assert "acme-demo[cli]==1.2.0" in cli
    assert f"vecten=={VERSIONS.vecten}" in cli


def test_a_project_requirement_reaches_only_its_components_lock(invoke, project, fakes):
    _edit_config(
        project,
        lambda doc: doc.__setitem__("dependencies", {"platform": {"python": ["polars==1.0"]}}),
    )
    assert invoke("lock").exit_code == 0
    assert "polars==1.0" in (project / "requirements-airflow.lock").read_text()
    assert "polars" not in (project / "requirements-mcp.lock").read_text()
    assert "polars" not in (project / "requirements.lock").read_text()


def test_each_compile_starts_from_the_committed_lock(invoke, project, fakes):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    committed = {c: (project / n).read_text() for c, n in locking.LOCK_FILES.items()}
    fakes["compiles"].clear()
    assert invoke("lock").exit_code == 0
    previous = sorted(c["previous"] for c in fakes["compiles"])
    assert previous == sorted(committed.values())


def test_upgrade_reaches_every_compile(invoke, project, fakes):
    assert invoke("lock", "--upgrade", "httpx", "--upgrade", "rich").exit_code == 0
    assert {tuple(c["upgrade"]) for c in fakes["compiles"]} == {("httpx", "rich")}


def test_a_failed_compile_changes_no_record_and_names_the_lock(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    before = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}

    def resolve(roots, root, *, constraints="", runtime=False, previous=None, upgrade=(), **kw):
        if any("[mcp]" in r for r in roots):
            raise Failure(ExitCode.TOOL_FAILED, "resolution failed")
        return "x==1\n"

    monkeypatch.setattr(extensions, "resolve_lock", resolve)
    r = invoke("lock", "--json")
    assert r.exit_code != 0
    assert "requirements-mcp.lock" in r.output
    assert {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()} == before


def test_the_first_lock_of_an_earlier_releases_project_migrates_its_records(invoke, project, fakes):
    _add_extension(project)
    _edit_config(
        project,
        lambda doc: doc["connectors"].append(
            {"name": "crm", "source": "rest", "dependencies": ["httpx==0.27.0"]}
        ),
    )
    (project / "requirements.txt").write_text("acme-demo==1.2.0\nhttpx==0.27.0\n")
    for name in locking.LOCK_FILES.values():
        (project / name).write_text("acme-demo==1.2.0\nhttpx==0.27.0\n")
    assert invoke("lock").exit_code == 0
    assert (project / "requirements.txt").read_text() == "acme-demo==1.2.0\n"
    assert (project / locking.RECORD_FILE).exists()
    assert locking.lock_problems(project, load_config(project)) == []


def test_a_pinned_extension_with_a_matching_record_is_not_inspected_again(invoke, project, fakes):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    fakes["inspected"].clear()
    assert invoke("lock").exit_code == 0
    assert fakes["inspected"] == []


def test_json_reports_packages_pinned_differently_across_locks(invoke, project, fakes, monkeypatch):
    def resolve(roots, root, *, constraints="", runtime=False, previous=None, upgrade=(), **kw):
        return "httpx==0.26.0\n" if runtime else "httpx==0.27.0\n"

    monkeypatch.setattr(extensions, "resolve_lock", resolve)
    r = invoke("lock", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["divergence"] == {
        "httpx": {"cli": "0.27.0", "mcp": "0.26.0", "platform": "0.26.0"}
    }


def test_resolve_lock_seeds_the_output_with_the_previous_lock_and_passes_upgrades(
    tmp_path, monkeypatch
):
    seen = {}

    def uv(args, root, cwd=None):
        out = Path(args[args.index("--output-file") + 1])
        seen["seeded"] = out.read_text()
        seen["args"] = args
        out.write_text("httpx==0.28.0\n")

    monkeypatch.setattr(extensions, "_uv", uv)
    text = extensions.resolve_lock(
        ["httpx"], tmp_path, previous="# vecten-lock: x\nhttpx==0.27.0\n", upgrade=("httpx",)
    )
    assert text == "httpx==0.28.0\n"
    assert seen["seeded"] == "# vecten-lock: x\nhttpx==0.27.0\n"
    assert seen["args"][seen["args"].index("--upgrade-package") + 1] == "httpx"


def test_a_constraint_the_pin_breaks_is_resolved_again(invoke, project, fakes):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    fakes["packages"]["acme-demo"] = ("2.0.0", ("platform",))
    _edit_config(project, lambda doc: doc.__setitem__("extensions", ["acme-demo>=2"]))
    fakes["inspected"].clear()
    assert invoke("lock").exit_code == 0
    assert fakes["inspected"] == ["acme-demo>=2"]
    assert (project / "requirements.txt").read_text() == "acme-demo==2.0.0\n"


@pytest.mark.parametrize(
    "dependency", ["polars\n--extra-index-url https://evil.example/simple", "x @ file:///tmp/x"]
)
def test_a_connector_dependency_that_is_not_a_plain_requirement_is_refused(
    invoke, project, fakes, dependency
):
    _edit_config(
        project,
        lambda doc: doc["connectors"].append(
            {"name": "crm", "source": "rest", "dependencies": [dependency]}
        ),
    )
    r = invoke("lock", "--json")
    assert r.exit_code != 0
    assert "crm" in r.output
    assert fakes["compiles"] == []


SCHEMA = {"properties": {"daily_cap": {"type": "integer", "default": 1000}}}


def _manifests(monkeypatch, value):
    from vecten_shared.declarations import parse_manifest

    monkeypatch.setattr(
        extensions,
        "load_manifests",
        lambda config, candidates=None: {"acme-demo": parse_manifest("acme-demo", value)},
    )


def test_the_record_holds_each_manifests_secrets_and_settings(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    _manifests(monkeypatch, {"secrets": ["MCP_ACME_TOKEN", "ACME_API_KEY"], "settings": SCHEMA})
    assert invoke("lock").exit_code == 0
    entry = json.loads((project / locking.RECORD_FILE).read_text())["extensions"]["acme-demo"]
    assert entry["secrets"] == ["ACME_API_KEY", "MCP_ACME_TOKEN"]
    assert entry["settings"] == SCHEMA


def test_an_unknown_manifest_key_is_reported(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    _manifests(monkeypatch, {"permissions": ["x"]})
    r = invoke("lock", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["unsupported"] == {"acme-demo": ["permissions"]}


def test_a_malformed_manifest_changes_no_record(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    before = (project / locking.RECORD_FILE).read_bytes()

    def bad(config, candidates=None):
        from vecten_shared.declarations import parse_manifest

        return {"acme-demo": parse_manifest("acme-demo", {"secrets": "ACME_API_KEY"})}

    monkeypatch.setattr(extensions, "load_manifests", bad)
    r = invoke("lock", "--json")
    assert r.exit_code == ExitCode.CONFIG
    assert "acme-demo" in r.output and "secrets" in r.output
    assert (project / locking.RECORD_FILE).read_bytes() == before


def test_lock_checks_settings_for_local_and_every_named_environment(
    invoke, project, fakes, monkeypatch
):
    _add_extension(project)
    _manifests(monkeypatch, {"settings": SCHEMA})
    _edit_config(
        project,
        lambda doc: doc.__setitem__(
            "settings", {"acme-demo": {"env": {"prod": {"daily_cap": "500"}}}}
        ),
    )
    r = invoke("lock", "--json")
    assert r.exit_code == ExitCode.CONFIG
    assert "settings.acme-demo.daily_cap" in r.output and "prod" in r.output


def test_a_secret_for_a_component_the_project_lacks_is_reported(
    invoke, project, fakes, monkeypatch
):
    _add_extension(project)
    _edit_config(project, lambda doc: doc.__setitem__("components", ["platform"]))
    _manifests(monkeypatch, {"secrets": ["MCP_ACME_TOKEN", "ACME_API_KEY"]})
    r = invoke("lock", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["undelivered"] == ["MCP_ACME_TOKEN"]
    assert "ACME_API_KEY" in data["missing_locally"]


def test_two_manifests_in_one_distribution_are_refused():
    from vecten_shared.config import VectenConfig

    dist = SimpleNamespace(name="acme-demo", version="1.0")
    points = [
        SimpleNamespace(name=n, dist=dist, group="vecten.manifest", load=lambda: {})
        for n in ("one", "two")
    ]
    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "a", "scaffold_version": "0.1.0"},
            "extensions": ["acme-demo"],
        }
    )
    with pytest.raises(Failure, match="acme-demo"):
        extensions.load_manifests(config, candidates=points)


def test_lock_names_secrets_to_set_and_secrets_not_delivered(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    _edit_config(project, lambda doc: doc.__setitem__("components", ["platform"]))
    _manifests(monkeypatch, {"secrets": ["MCP_ACME_TOKEN", "ACME_API_KEY"]})
    r = invoke("lock")
    assert r.exit_code == 0, r.output
    assert "vecten secret set ACME_API_KEY" in r.output
    assert "MCP_ACME_TOKEN" in r.output and "not delivered" in r.output


def test_extension_add_names_the_secrets_to_set(invoke, project, fakes, monkeypatch):
    _manifests(monkeypatch, {"secrets": ["ACME_API_KEY"]})
    r = invoke("extension", "add", "acme-demo", "--json")
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["missing_secrets"] == ["ACME_API_KEY"]
    r = invoke("extension", "add", "acme-demo")
    assert "vecten secret set ACME_API_KEY" in r.output


def test_two_extensions_may_name_their_manifest_entry_point_alike():
    from vecten_shared.config import VectenConfig

    def point(dist_name, secrets):
        dist = SimpleNamespace(name=dist_name, version="1.0")
        return SimpleNamespace(
            name="manifest",
            dist=dist,
            group="vecten.manifest",
            load=lambda: {"secrets": secrets},
        )

    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "a", "scaffold_version": "0.1.0"},
            "extensions": ["acme-one", "acme-two"],
        }
    )
    found = extensions.load_manifests(
        config, candidates=[point("acme-one", ["ONE_TOKEN"]), point("acme-two", ["TWO_TOKEN"])]
    )
    assert found["acme-one"].secrets == ("ONE_TOKEN",)
    assert found["acme-two"].secrets == ("TWO_TOKEN",)


# --- in-repo extensions ------------------------------------------------------


def _register(root):
    make(root)
    doc = tomllib.loads((root / "vecten.toml").read_text())
    doc["extensions"] = ["acme-portal"]
    (root / "vecten.toml").write_text(tomli_w.dumps(doc))
    (root / "requirements.txt").write_text("./extensions/acme-portal\n")


def test_extension_add_registers_an_in_repo_extension_by_its_path(invoke, project, fakes):
    make(project)
    r = invoke("extension", "add", "./extensions/acme-portal", "--json")
    assert r.exit_code == 0, r.output
    assert tomllib.loads((project / "vecten.toml").read_text())["extensions"] == ["acme-portal"]
    assert (project / "requirements.txt").read_text() == "./extensions/acme-portal\n"
    entry = json.loads((project / locking.RECORD_FILE).read_text())["extensions"]["acme-portal"]
    assert entry["source"] == {"path": "extensions/acme-portal"}
    assert entry["version"] == "0.1.0"
    assert entry["extras"] == ["platform"]
    assert len(entry["metadata_sha256"]) == 64
    assert entry["build_requires"]
    assert fakes["inspected"] == []


def test_each_lock_is_resolved_with_the_extension_as_input_and_holds_no_path(
    invoke, project, fakes
):
    _register(project)
    assert invoke("lock").exit_code == 0
    roots = {tuple(c["roots"]) for c in fakes["compiles"] if not c.get("build")}
    assert any("./extensions/acme-portal[platform]" in r for r in roots)
    assert all(c.get("cwd") == project for c in fakes["compiles"])
    for name in locking.LOCK_FILES.values():
        text = (project / name).read_text()
        assert "./extensions" not in text and "acme-portal" not in text


def test_the_cli_installs_the_lock_then_the_extension_editable(invoke, project, fakes):
    _register(project)
    assert invoke("lock").exit_code == 0
    lock, editable = fakes["installed"][-1]
    assert editable == ("extensions/acme-portal",)


def test_a_code_edit_keeps_the_locks_and_a_metadata_edit_makes_them_stale(invoke, project, fakes):
    _register(project)
    assert invoke("lock").exit_code == 0
    module = project / "extensions/acme-portal/src/acme_portal/__init__.py"
    module.write_text("skill = 'changed'\n")
    assert locking.lock_problems(project, load_config(project)) == []
    metadata = project / "extensions/acme-portal/pyproject.toml"
    metadata.write_text(metadata.read_text().replace("httpx>=0.27", "httpx>=0.28"))
    assert "requirements-airflow.lock" in locking.lock_problems(project, load_config(project))


def test_a_lock_that_pins_the_in_repo_name_from_an_index_is_refused(
    invoke, project, fakes, monkeypatch
):
    _register(project)

    def resolve(roots, root, **kw):
        return "acme-portal==9.9.9\nhelper==1.0\n"

    monkeypatch.setattr(extensions, "resolve_lock", resolve)
    r = invoke("lock", "--json")
    assert r.exit_code == ExitCode.REFUSED
    assert "acme-portal" in r.output


def test_a_registered_in_repo_extension_that_breaks_a_rule_stops_the_lock(invoke, project, fakes):
    _register(project)
    (project / "extensions/acme-portal/src/link").symlink_to(project)
    r = invoke("lock", "--json")
    assert r.exit_code != 0
    assert "symbolic link" in r.output
