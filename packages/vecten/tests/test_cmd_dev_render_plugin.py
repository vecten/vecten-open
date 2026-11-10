import json
import shutil
from pathlib import Path

import pytest

from vecten.knowhow import load_skill, skill_names
from vecten_shared.output import ExitCode

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copytree(REPO / "plugins", root / "plugins")
    shutil.copytree(REPO / ".claude-plugin", root / ".claude-plugin")
    (root / "README.md").write_text("# Vecten Open\n")
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "agent-guide.md", root / "docs" / "agent-guide.md")
    return root


def check(invoke, root):
    return invoke("dev", "render-plugin", "--repo", str(root), "--check", "--json")


def test_committed_copy_is_in_sync(invoke, repo):
    r = check(invoke, repo)
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["changed"] == []


def test_modified_skill_is_named_then_rewritten(invoke, repo):
    skill = repo / "plugins" / "vecten" / "skills" / "model" / "SKILL.md"
    skill.write_text(skill.read_text() + "drift\n")
    r = check(invoke, repo)
    assert r.exit_code == ExitCode.CONFIG
    assert json.loads(r.stdout)["changed"] == ["plugins/vecten/skills/model/SKILL.md"]
    assert skill.read_text().endswith("drift\n")
    r = invoke("dev", "render-plugin", "--repo", str(repo), "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["changed"] == ["plugins/vecten/skills/model/SKILL.md"]
    assert skill.read_text() == load_skill("model")
    assert check(invoke, repo).exit_code == 0


def test_stale_skill_and_guide_document_drift_are_repaired(invoke, repo):
    stale = repo / "plugins" / "vecten" / "skills" / "renamed"
    stale.mkdir()
    (stale / "SKILL.md").write_text("---\nname: renamed\n---\n")
    guide = repo / "docs" / "agent-guide.md"
    guide.write_text(guide.read_text().replace("How I work", "How we work"))
    r = check(invoke, repo)
    assert r.exit_code == ExitCode.CONFIG
    assert json.loads(r.stdout)["changed"] == [
        "plugins/vecten/skills/renamed",
        "docs/agent-guide.md",
    ]
    assert invoke("dev", "render-plugin", "--repo", str(repo)).exit_code == 0
    assert not stale.exists()
    assert sorted(p.name for p in (repo / "plugins" / "vecten" / "skills").iterdir()) == (
        skill_names()
    )
    assert guide.read_text() == (REPO / "docs" / "agent-guide.md").read_text()
    assert check(invoke, repo).exit_code == 0


def test_empty_repo_is_written_from_scratch(invoke, tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "README.md").write_text("# Vecten Open\n")
    r = invoke("dev", "render-plugin", "--repo", str(root), "--json")
    assert r.exit_code == 0, r.stdout
    changed = json.loads(r.stdout)["changed"]
    assert "plugins/vecten/.claude-plugin/plugin.json" in changed
    assert ".claude-plugin/marketplace.json" in changed
    assert "docs/agent-guide.md" in changed
    assert (root / "docs" / "agent-guide.md").read_text() == (
        REPO / "docs" / "agent-guide.md"
    ).read_text()
    assert "plugins/vecten/.mcp.json" not in changed
    assert not (root / "plugins" / "vecten" / ".mcp.json").exists()
    assert check(invoke, root).exit_code == 0


def test_marketplace_drift_is_named(invoke, repo):
    marketplace = repo / ".claude-plugin" / "marketplace.json"
    marketplace.write_text(marketplace.read_text().replace('"vecten"', '"other"', 1))
    r = check(invoke, repo)
    assert r.exit_code == ExitCode.CONFIG
    assert json.loads(r.stdout)["changed"] == [".claude-plugin/marketplace.json"]


def test_dotfiles_beside_the_skills_are_not_stale(invoke, repo):
    skills = repo / "plugins" / "vecten" / "skills"
    (skills / ".DS_Store").write_text("finder")
    (skills / "model" / ".DS_Store").write_text("finder")
    r = check(invoke, repo)
    assert r.exit_code == 0, r.stdout
    assert invoke("dev", "render-plugin", "--repo", str(repo)).exit_code == 0
    assert (skills / ".DS_Store").is_file() and (skills / "model" / ".DS_Store").is_file()


def test_absent_guide_document_fails_check(invoke, repo):
    (repo / "docs" / "agent-guide.md").unlink()
    r = check(invoke, repo)
    assert r.exit_code == ExitCode.CONFIG
    assert json.loads(r.stdout)["changed"] == ["docs/agent-guide.md"]
    assert not (repo / "docs" / "agent-guide.md").exists()


def test_refuses_outside_a_checkout(invoke, tmp_path):
    r = invoke("dev", "render-plugin", "--repo", str(tmp_path / "missing"), "--json")
    assert r.exit_code == ExitCode.USAGE
