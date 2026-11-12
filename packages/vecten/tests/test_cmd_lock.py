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


BOT = {
    "command": ["python", "-m", "acme_bot.server"],
    "port": 8000,
    "health": {"path": "/healthz"},
    "routes": {"webhook": {"path": "/api/messages", "access": "public"}},
}


def _services(monkeypatch, value):
    from vecten_shared.services import parse_service

    monkeypatch.setattr(
        extensions,
        "load_services",
        lambda config, candidates=None: {
            "acme-demo": {"bot": parse_service("acme-demo", "bot", value)}
        },
    )


def test_the_record_holds_each_service_declaration(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    _services(monkeypatch, BOT)
    assert invoke("lock").exit_code == 0
    entry = json.loads((project / locking.RECORD_FILE).read_text())["extensions"]["acme-demo"]
    assert entry["services"]["bot"]["command"] == ["python", "-m", "acme_bot.server"]
    assert entry["services"]["bot"]["routes"]["webhook"]["path"] == "/api/messages"


def test_an_unknown_service_key_is_reported(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    _manifests(monkeypatch, {"permissions": ["x"]})
    _services(monkeypatch, {**BOT, "replicas": 2})
    r = invoke("lock", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["unsupported"] == {
        "acme-demo": ["permissions", "services.bot.replicas"]
    }
    assert "services.bot.replicas" in invoke("lock").output


def test_a_route_under_the_reserved_prefix_changes_no_record(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    before = (project / locking.RECORD_FILE).read_bytes()
    routes = {"hook": {"path": "/_vecten/hooks", "access": "public"}}

    def bad(config, candidates=None):
        from vecten_shared.services import parse_service

        return {"acme-demo": {"bot": parse_service("acme-demo", "bot", {**BOT, "routes": routes})}}

    monkeypatch.setattr(extensions, "load_services", bad)
    r = invoke("lock", "--json")
    assert r.exit_code == ExitCode.CONFIG
    assert "acme-demo" in r.output and "bot" in r.output and "path" in r.output
    assert (project / locking.RECORD_FILE).read_bytes() == before


def _service_point(dist_name, name, load):
    dist = SimpleNamespace(name=dist_name, version="1.0")
    return SimpleNamespace(name=name, dist=dist, group="vecten.services", load=load)


def _config(*names):
    from vecten_shared.config import VectenConfig

    return VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "a", "scaffold_version": "0.1.0"},
            "extensions": list(names),
        }
    )


def test_services_are_read_per_listed_extension():
    points = [
        _service_point("Acme_One", "bot", lambda: BOT),
        _service_point("acme-two", "bot", lambda: BOT),
        _service_point("unlisted", "bot", lambda: BOT),
    ]
    found = extensions.load_services(_config("acme-one", "acme-two"), candidates=points)
    assert set(found) == {"acme-one", "acme-two"}
    assert found["acme-one"]["bot"].service.port == 8000


def test_a_service_that_cannot_load_names_its_extension_and_service():
    def broken():
        raise ImportError("streamlit")

    points = [_service_point("acme-one", "portal", broken)]
    with pytest.raises(Failure, match="acme-one.*portal.*ImportError"):
        extensions.load_services(_config("acme-one"), candidates=points)


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


def test_an_in_repo_extension_stays_in_repo_when_its_requirements_line_is_lost(
    invoke, project, fakes
):
    _register(project)
    assert invoke("lock").exit_code == 0
    (project / "requirements.txt").write_text("")
    assert invoke("lock").exit_code == 0
    assert fakes["inspected"] == []
    assert (project / "requirements.txt").read_text() == "./extensions/acme-portal\n"


def test_adding_by_name_an_extension_kept_in_the_project_is_refused(invoke, project, fakes):
    make(project)
    r = invoke("extension", "add", "acme-portal", "--json")
    assert r.exit_code != 0
    assert "./extensions/acme-portal" in r.output
    assert fakes["inspected"] == []


def test_switching_requirements_to_an_index_pin_makes_the_locks_stale_and_relocks_by_name(
    invoke, project, fakes
):
    _register(project)
    assert invoke("lock").exit_code == 0
    import shutil

    shutil.rmtree(project / "extensions")
    (project / "requirements.txt").write_text("acme-portal==0.1.0\n")
    assert "requirements-airflow.lock" in locking.lock_problems(project, load_config(project))
    fakes["packages"]["acme-portal"] = ("0.1.0", ())
    assert invoke("lock").exit_code == 0
    entry = json.loads((project / locking.RECORD_FILE).read_text())["extensions"]["acme-portal"]
    assert "source" not in entry


def test_lock_refuses_a_binding_of_an_undeclared_service(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    before = (project / locking.RECORD_FILE).read_bytes()
    _edit_config(project, lambda doc: doc.__setitem__("services", {"acme-demo": {"portal": {}}}))
    r = invoke("lock", "--json")
    assert r.exit_code == ExitCode.CONFIG, r.output
    assert "services.acme-demo.portal" in r.output
    assert (project / locking.RECORD_FILE).read_bytes() == before


PORTAL = {
    **BOT,
    "routes": {
        "webhook": {"path": "/api/messages", "access": "public"},
        "admin": {"path": "/", "access": "login", "groups": ["default"]},
    },
}


def _declared(monkeypatch, services):
    from vecten_shared.services import parse_service

    monkeypatch.setattr(
        extensions,
        "load_services",
        lambda config, candidates=None, only=None: {
            "acme-demo": {n: parse_service("acme-demo", n, v) for n, v in services.items()}
        },
    )


def test_extension_add_binds_each_service_from_its_suggestions(invoke, project, fakes, monkeypatch):
    _declared(monkeypatch, {"bot": PORTAL})
    r = invoke("extension", "add", "acme-demo", "--json")
    assert r.exit_code == 0, r.output
    text = (project / "vecten.toml").read_text()
    assert "[services.acme-demo.bot]" in text
    assert "[services.acme-demo.bot.routes.webhook]" in text
    binding = load_config(project).services["acme-demo"]["bot"]
    assert binding.hostname == "bot" and binding.host is None
    assert binding.routes["webhook"].access == "public"
    assert (binding.routes["admin"].access, binding.routes["admin"].groups) == (
        "login",
        ["default"],
    )
    assert json.loads(r.stdout)["public_routes"] == ["acme-demo.bot route webhook"]
    _edit_config(project, lambda doc: doc.pop("services"))
    out = invoke("extension", "add", "acme-demo").output
    assert "public, reachable by anyone: acme-demo.bot route webhook" in out


def test_a_service_without_routes_gets_no_hostname(invoke, project, fakes, monkeypatch):
    _declared(monkeypatch, {"worker": {"command": ["run"], "health": {"command": ["true"]}}})
    assert invoke("extension", "add", "acme-demo").exit_code == 0
    assert load_config(project).services["acme-demo"]["worker"].hostname is None


def test_a_reserved_or_taken_label_falls_back_to_extension_and_service(
    invoke, project, fakes, monkeypatch
):
    _declared(monkeypatch, {"mcp": PORTAL, "www": PORTAL})
    assert invoke("extension", "add", "acme-demo").exit_code == 0, invoke("lock").output
    services = load_config(project).services["acme-demo"]
    assert services["mcp"].hostname == "acme-demo-mcp"
    assert services["www"].hostname == "acme-demo-www"


def test_upgrade_keeps_the_projects_binding_and_names_the_difference(
    invoke, project, fakes, monkeypatch
):
    _declared(monkeypatch, {"bot": PORTAL})
    assert invoke("extension", "add", "acme-demo").exit_code == 0
    changed = {
        **PORTAL,
        "routes": {
            "webhook": {"path": "/api/messages", "access": "login", "groups": ["default"]},
            "admin": {"path": "/", "access": "login", "groups": ["default"]},
            "export": {"path": "/export", "access": "public"},
        },
    }
    _declared(monkeypatch, {"bot": changed})
    r = invoke("extension", "add", "acme-demo", "--json")
    assert r.exit_code == 0, r.output
    binding = load_config(project).services["acme-demo"]["bot"]
    assert binding.routes["webhook"].access == "public"
    assert "export" not in binding.routes
    notes = json.loads(r.stdout)["binding_notes"]
    assert "acme-demo.bot route webhook: bound public, suggested login" in notes
    assert "acme-demo.bot route export: declared, not bound" in notes


def test_a_login_route_is_left_unbound_without_the_mcp(invoke, project, fakes, monkeypatch):
    _edit_config(project, lambda doc: doc.__setitem__("components", ["platform"]))
    _declared(monkeypatch, {"bot": PORTAL})
    r = invoke("extension", "add", "acme-demo", "--json")
    assert r.exit_code == 0, r.output
    assert set(load_config(project).services["acme-demo"]["bot"].routes) == {"webhook"}
    assert (
        "acme-demo.bot route admin: login needs the mcp component; not bound"
        in (json.loads(r.stdout)["binding_notes"])
    )


def test_a_service_for_a_component_the_project_lacks_is_not_bound(
    invoke, project, fakes, monkeypatch
):
    _edit_config(project, lambda doc: doc.__setitem__("components", ["platform"]))
    _declared(monkeypatch, {"tool": {**BOT, "components": ["mcp"]}})
    r = invoke("extension", "add", "acme-demo", "--json")
    assert r.exit_code == 0, r.output
    assert load_config(project).services == {}
    assert (
        "acme-demo.tool: runs from mcp, which the project does not enable; not bound"
        in (json.loads(r.stdout)["binding_notes"])
    )


def test_a_service_only_the_mcp_runs_is_placed_there(invoke, project, fakes, monkeypatch):
    _declared(monkeypatch, {"tool": {**BOT, "components": ["mcp"]}})
    assert invoke("extension", "add", "acme-demo").exit_code == 0
    assert load_config(project).services["acme-demo"]["tool"].host == "mcp"


def test_a_hostname_needing_deploy_is_named_once_the_project_deploys(
    invoke, project, fakes, monkeypatch
):
    deploy = {
        "cloud": "aws",
        "region": "eu-west-1",
        "domain": "data.acme.com",
        "dns_zone": "acme.com",
        "github_repository": "acme/data",
    }
    _edit_config(project, lambda doc: doc.__setitem__("deploy", deploy))
    _declared(monkeypatch, {"bot": PORTAL})
    r = invoke("extension", "add", "acme-demo", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["hostnames"] == ["bot.data.acme.com"]
    _edit_config(project, lambda doc: doc.pop("services"))
    out = invoke("extension", "add", "acme-demo").output
    assert "bot.data.acme.com" in out and "vecten deploy" in out


OVERVIEW = {
    "title": "Sales overview",
    "description": "Weekly bookings",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme_demo/app.py",
    "datasets": ["orders"],
    "groups": ["default"],
}


def _apps(monkeypatch, apps):
    from vecten_shared.apps import parse_app

    monkeypatch.setattr(
        extensions,
        "load_apps",
        lambda config, candidates=None, only=None: {
            "acme-demo": {n: parse_app("acme-demo", n, v) for n, v in apps.items()}
        },
    )


def _app_point(dist_name, name, load):
    dist = SimpleNamespace(name=dist_name, version="1.0")
    return SimpleNamespace(name=name, dist=dist, group="vecten.apps", load=load)


def test_the_record_holds_each_app_declaration(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    _apps(monkeypatch, {"overview": OVERVIEW})
    r = invoke("lock", "--json")
    assert r.exit_code == 0, r.output
    entry = json.loads((project / locking.RECORD_FILE).read_text())["extensions"]["acme-demo"]
    assert entry["apps"]["overview"]["entry"] == "acme_demo/app.py"
    assert entry["apps"]["overview"]["datasets"] == ["orders"]
    assert locking.read_record(project)["acme-demo"].apps["overview"].kind == "container"


def test_an_unknown_app_key_is_reported(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    _apps(monkeypatch, {"overview": {**OVERVIEW, "theme": "dark"}})
    r = invoke("lock", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["unsupported"] == {"acme-demo": ["apps.overview.theme"]}


def test_an_app_without_datasets_changes_no_record(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    assert invoke("lock").exit_code == 0
    before = (project / locking.RECORD_FILE).read_bytes()

    def bad(config, candidates=None, only=None):
        from vecten_shared.apps import parse_app

        return {
            "acme-demo": {
                "overview": parse_app("acme-demo", "overview", OVERVIEW | {"datasets": []})
            }
        }

    monkeypatch.setattr(extensions, "load_apps", bad)
    r = invoke("lock", "--json")
    assert r.exit_code == ExitCode.CONFIG
    assert "acme-demo" in r.output and "overview" in r.output and "datasets" in r.output
    assert (project / locking.RECORD_FILE).read_bytes() == before


def test_apps_are_read_per_listed_extension():
    points = [
        _app_point("Acme_One", "overview", lambda: OVERVIEW),
        _app_point("unlisted", "overview", lambda: OVERVIEW),
    ]
    found = extensions.load_apps(_config("acme-one"), candidates=points)
    assert set(found) == {"acme-one"}
    assert found["acme-one"]["overview"].app.title == "Sales overview"


def test_an_app_that_cannot_load_names_its_extension_and_app():
    def broken():
        raise ImportError("streamlit")

    points = [_app_point("acme-one", "overview", broken)]
    with pytest.raises(Failure, match="acme-one.*app overview.*ImportError"):
        extensions.load_apps(_config("acme-one"), candidates=points)


def test_a_service_named_as_an_apps_service_is_refused(invoke, project, fakes, monkeypatch):
    _add_extension(project)
    _services(monkeypatch, BOT)
    from vecten_shared.services import parse_service

    monkeypatch.setattr(
        extensions,
        "load_services",
        lambda config, candidates=None, only=None: {
            "acme-demo": {"app-overview": parse_service("acme-demo", "app-overview", BOT)}
        },
    )
    _apps(monkeypatch, {"overview": OVERVIEW})
    r = invoke("lock")
    assert r.exit_code == ExitCode.CONFIG
    assert "service app-overview" in r.output and "app overview" in r.output
