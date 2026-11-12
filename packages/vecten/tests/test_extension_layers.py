import importlib.metadata
import shutil
from pathlib import Path

import pytest

from vecten_shared.config import VectenConfig, save_config
from vecten_shared.extensions import GROUPS
from vecten_shared.output import Failure

FIXTURE = Path(__file__).parent / "fixtures" / "extension"


@pytest.fixture
def extension(tmp_path, monkeypatch):
    import configparser
    import tomllib

    site = tmp_path / "site"
    shutil.copytree(FIXTURE / "src", site)
    doc = tomllib.loads((FIXTURE / "pyproject.toml").read_text())
    info = site / "vecten_test_extension-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: vecten-test-extension\nVersion: 1.0\n"
    )
    parser = configparser.ConfigParser()
    parser.read_dict(doc["project"]["entry-points"])
    with (info / "entry_points.txt").open("w") as stream:
        parser.write(stream)
    monkeypatch.syspath_prepend(str(site))
    config = VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "extensions": ["vecten-test-extension==1.0"],
            "mcp": {"auth": "fixture"},
        }
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "osi").mkdir()
    save_config(root, config)
    yield root, config
    import sys

    sys.modules.pop("vecten_test_extension", None)


def test_all_twelve_groups_land(extension):
    from vecten.connectors.extensions import extension_connectors
    from vecten.extension_skills import link_extension_skills
    from vecten_mcp.extensions import McpExtensions
    from vecten_platform.dbt.deps import extension_packages
    from vecten_platform.extensions import dag_files
    from vecten_shared.osi.documents import list_documents, load_document, merge
    from vecten_shared.osi.validate import validate_osi

    root, config = extension
    dist = importlib.metadata.distribution("vecten-test-extension")
    assert {p.group for p in dist.entry_points} == set(GROUPS)
    connector = extension_connectors(config)["fixture"]
    assert connector.package == dist.name
    assert connector.groups["daily"].schedule == "0 6 * * *"
    assert connector.groups["daily"].timezone == "Europe/Warsaw"
    assert extension_packages(config)[0][1].name == "dbt"
    assert [p.name for p in dag_files(config)] == ["fixture.py"]
    assert (
        "fixture_dataset"
        in merge([load_document(p) for p in list_documents(root / "osi")]).datasets
    )
    assert not validate_osi(root / "osi", warehouse_kind="postgres").errors
    registry = McpExtensions.from_config(config)
    builtins = {
        "describe",
        "search",
        "sql-exec",
        "query_metrics",
        "report_feedback",
        "verify_receipt",
    }
    assert set(registry.tools_for({"analyst"})) == builtins | {"fixture"}
    assert registry.plugin_for("fixture") == "vecten-test-extension"
    assert set(registry.tools_for(set())) == builtins
    assert registry.identity_backend("fixture")({}) == {"subject": "fixture"}
    assert registry.provision("fixture", "alice") == "alice"
    registry.emit_audit({"receipt": "one"})
    from vecten_test_extension import rows

    assert rows == [{"receipt": "one"}]
    link_extension_skills(root, config)
    for directory in (root / "skills", root / "plugins" / "vecten" / "skills"):
        assert (directory / "fixture").is_symlink()
        assert (directory / "fixture" / "SKILL.md").is_file()


def test_missing_backend_fails_app_startup(tmp_path):
    from vecten_mcp.app import create_app

    config = VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "mcp": {"auth": "saml"},
        }
    )
    save_config(tmp_path, config)
    with pytest.raises(Failure, match="saml.*not installed"):
        create_app(tmp_path)


def test_resource_collisions_and_semantic_duplicates(extension):
    from vecten.extension_skills import link_extension_skills
    from vecten_shared.osi.validate import validate_osi

    root, config = extension
    user_skill = root / "skills" / "fixture"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("user-owned")
    with pytest.raises(Failure, match="overwrite"):
        link_extension_skills(root, config)
    assert (user_skill / "SKILL.md").read_text() == "user-owned"
    from vecten_shared.osi.documents import list_documents

    (root / "osi" / "duplicate.json").write_text(list_documents(root / "osi")[0].read_text())
    assert any(
        e.code == "duplicate_name"
        for e in validate_osi(root / "osi", warehouse_kind="postgres").errors
    )


def test_app_registers_extensions(extension):
    from vecten_mcp.app import create_app

    root, _ = extension
    assert create_app(root).state.extensions.identity_backend("fixture")({})["subject"] == "fixture"


def test_answer_accepts_installed_identity_backend(extension):
    from vecten.intake.facts import set_mcp_fact

    root, _ = extension
    assert set_mcp_fact(root, "mcp.auth", "fixture") == "fixture"


def test_skill_links_are_ignored_and_parent_symlinks_refused(extension, tmp_path):
    from vecten.extension_skills import link_extension_skills

    root, config = extension
    link_extension_skills(root, config)
    ignore = (root / ".gitignore").read_text()
    assert "/skills/fixture\n" in ignore
    assert "/plugins/vecten/skills/fixture\n" in ignore
    import shutil

    shutil.rmtree(root / "plugins")
    other = tmp_path / "other"
    other.mkdir()
    (root / "plugins").symlink_to(other, target_is_directory=True)
    with pytest.raises(Failure, match="symbolic link"):
        link_extension_skills(root, config)
    assert list(other.iterdir()) == []


@pytest.mark.parametrize("managed,remove", [(False, False), (True, False), (True, True)])
def test_unowned_or_modified_skill_symlinks_are_preserved(extension, tmp_path, managed, remove):
    from vecten.extension_skills import link_extension_skills

    root, config = extension
    target = root / "skills/fixture"
    if managed:
        link_extension_skills(root, config)
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "SKILL.md").write_text("user content")
    target.symlink_to(owned, target_is_directory=True)
    if remove:
        config.extensions = []
    with pytest.raises(Failure, match="project skill"):
        link_extension_skills(root, config)
    assert target.resolve() == owned
    assert (target / "SKILL.md").read_text() == "user content"


def test_owned_skill_links_can_be_refreshed_and_removed(extension):
    from vecten.extension_skills import link_extension_skills

    root, config = extension
    link_extension_skills(root, config)
    link_extension_skills(root, config)
    config.extensions = []
    link_extension_skills(root, config)
    assert not (root / "skills/fixture").is_symlink()


def test_semantic_digest_is_stable_when_extension_environment_moves(
    extension, tmp_path, monkeypatch
):
    from vecten.next.semantic import osi_digest
    from vecten_shared.extensions import extension_paths

    root, config = extension
    original = extension_paths(config, "vecten.osi")["fixture"]
    moved = tmp_path / "new-venv/osi"
    shutil.copytree(original, moved)
    before = osi_digest(root / "osi")
    monkeypatch.setattr("vecten_shared.extensions.extension_paths", lambda *a: {"fixture": moved})
    assert osi_digest(root / "osi") == before
    (moved / "fixture.json").write_text("{}")
    assert osi_digest(root / "osi") != before


def test_the_manifest_reaches_the_record_the_laptop_stack_and_the_read_api(extension, monkeypatch):
    from vecten.extensions import load_manifests
    from vecten.stack.compose import generate_compose
    from vecten_shared import context
    from vecten_shared.extension_record import Extension, record_text

    root, config = extension
    manifest = load_manifests(config)["vecten-test-extension"]
    assert manifest.secrets == ("FIXTURE_TOKEN", "MCP_FIXTURE_TOKEN")
    record = {
        "vecten-test-extension": Extension(
            "1.0", (), secrets=manifest.secrets, settings=manifest.settings
        )
    }
    (root / "extensions.lock.json").write_text(record_text(record))
    stack = root / ".vecten" / "stack"
    stack.mkdir(parents=True)
    doc = generate_compose(config, "local", ["platform", "mcp"], stack)
    assert doc["services"]["airflow-scheduler"]["environment"]["FIXTURE_TOKEN"]
    assert doc["services"]["mcp-server"]["environment"]["MCP_FIXTURE_TOKEN"]

    monkeypatch.setenv("VECTEN_PROJECT_ROOT", str(root))
    monkeypatch.setenv("VECTEN_ENV", "local")
    monkeypatch.delenv("VECTEN_PROVIDER", raising=False)
    monkeypatch.setenv("FIXTURE_TOKEN", "dag-token")
    monkeypatch.setenv("MCP_FIXTURE_TOKEN", "tool-token")
    monkeypatch.setenv("VECTEN_COMPONENT", "platform")
    in_a_dag = context.extension("vecten-test-extension")
    assert in_a_dag.secret("FIXTURE_TOKEN") == "dag-token"
    assert in_a_dag.settings["greeting"] == "hello"
    monkeypatch.setenv("VECTEN_COMPONENT", "mcp")
    assert context.extension("vecten-test-extension").secret("MCP_FIXTURE_TOKEN") == "tool-token"


def test_the_stub_service_and_both_routes_reach_the_record_the_binding_and_compose(
    extension, monkeypatch
):
    from vecten.extensions import _bind_services, load_services
    from vecten.stack.compose import generate_compose
    from vecten_shared.config import load_config
    from vecten_shared.extension_record import Extension, parse_record, record_text

    root, config = extension
    declared = load_services(config)["vecten-test-extension"]["portal"]
    assert declared.unknown == ()
    assert {n: r.access for n, r in declared.service.routes.items()} == {
        "hook": "public",
        "home": "login",
    }
    entry = Extension("1.0", (), services={"portal": declared.service})
    (root / "extensions.lock.json").write_text(record_text({"vecten-test-extension": entry}))
    assert parse_record((root / "extensions.lock.json").read_text())[
        "vecten-test-extension"
    ].services == {"portal": declared.service}

    import tomlkit

    doc = tomlkit.parse((root / "vecten.toml").read_text())
    _bind_services(doc, config, "vecten-test-extension", entry)
    (root / "vecten.toml").write_text(tomlkit.dumps(doc))
    bound = load_config(root)
    assert set(bound.services["vecten-test-extension"]["portal"].routes) == {"hook", "home"}
    stack = root / ".vecten" / "stack"
    stack.mkdir(parents=True)
    compose = generate_compose(bound, "local", ["platform", "mcp"], stack)
    assert compose["services"]["svc-vecten-test-extension-portal"]["command"] == [
        "python",
        "-m",
        "vecten_test_extension.portal",
    ]
