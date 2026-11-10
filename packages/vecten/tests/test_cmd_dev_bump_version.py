import json
import re
import shutil
import tomllib
from pathlib import Path

import pytest

from vecten.version_bump import VERSION_FILES
from vecten_shared.output import ExitCode
from vecten_shared.versions import VERSIONS

REPO = Path(__file__).resolve().parents[3]
DISTRIBUTIONS = ("vecten", "vecten-platform", "vecten-mcp", "vecten-shared")
NEXT = "99.0.0rc99"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for rel in VERSION_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, root / rel)
    return root


def snapshot(root):
    return {rel: (root / rel).read_text() for rel in VERSION_FILES}


def bump(invoke, root, version, *extra):
    return invoke("dev", "bump-version", version, "--repo", str(root), *extra, "--json")


def test_the_twelve_files_are_the_ones_a_bump_edits():
    assert len(VERSION_FILES) == 12
    for rel in VERSION_FILES:
        assert VERSIONS.vecten in (REPO / rel).read_text(), rel


def test_bump_rewrites_every_version_and_pin(invoke, repo):
    r = bump(invoke, repo, NEXT)
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert (doc["from"], doc["to"], doc["written"]) == (VERSIONS.vecten, NEXT, True)
    assert sorted(doc["files"]) == sorted(VERSION_FILES)
    for dist in DISTRIBUTIONS:
        package = repo / "packages" / dist
        project = tomllib.loads((package / "pyproject.toml").read_text())["project"]
        assert project["version"] == NEXT, dist
        internal = [d for d in project["dependencies"] if d.startswith("vecten")]
        assert all(d.endswith(f"=={NEXT}") for d in internal), internal
        init = (package / "src" / dist.replace("-", "_") / "__init__.py").read_text()
        assert init == f'__version__ = "{NEXT}"\n', dist
    table = tomllib.loads(
        (repo / "packages/vecten-shared/src/vecten_shared/versions.toml").read_text()
    )
    assert table["vecten"] == NEXT and table["terraform"]["module_ref"] == f"v{NEXT}"
    plugin = json.loads((repo / "plugins/vecten/.claude-plugin/plugin.json").read_text())
    marketplace = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    assert plugin["version"] == marketplace["plugins"][0]["version"] == NEXT
    lock = tomllib.loads((repo / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"] if p["name"] in DISTRIBUTIONS}
    assert locked == dict.fromkeys(DISTRIBUTIONS, NEXT)


def test_check_lists_the_files_and_writes_nothing(invoke, repo):
    before = snapshot(repo)
    r = bump(invoke, repo, NEXT, "--check")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["written"] is False and sorted(doc["files"]) == sorted(VERSION_FILES)
    assert snapshot(repo) == before


@pytest.mark.parametrize(
    "version",
    ["not-a-version", "0.1.0-rc99", VERSIONS.vecten, "0.0.9"],
    ids=["invalid", "not-canonical", "same", "lower"],
)
def test_wrong_version_is_refused_before_anything_is_written(invoke, repo, version):
    before = snapshot(repo)
    r = bump(invoke, repo, version)
    assert r.exit_code == ExitCode.USAGE, r.stdout
    assert snapshot(repo) == before


def test_missing_anchor_fails_before_anything_is_written(invoke, repo):
    init = repo / "packages/vecten-mcp/src/vecten_mcp/__init__.py"
    init.write_text('__version__ = "0.0.1"\n')
    before = snapshot(repo)
    r = bump(invoke, repo, NEXT)
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert "packages/vecten-mcp/src/vecten_mcp/__init__.py" in json.loads(r.stdout)["error"]
    assert snapshot(repo) == before


def test_other_versions_that_equal_the_current_one_are_left_alone(invoke, repo):
    old = VERSIONS.vecten
    table = repo / "packages/vecten-shared/src/vecten_shared/versions.toml"
    table.write_text(
        re.sub(r'(?m)^vecten_convention = ".*"$', f'vecten_convention = "{old}"', table.read_text())
    )
    with (repo / "uv.lock").open("a") as lock:
        lock.write(
            f'\n[[package]]\nname = "other-package"\nversion = "{old}"\n'
            f'sdist = {{ url = "https://example.invalid/other_package-1{old}.tar.gz" }}\n'
        )
    with (repo / "packages/vecten/pyproject.toml").open("a") as pyproject:
        pyproject.write(f'\n[tool.other]\npin = "other-package>={old}"\n')
    r = bump(invoke, repo, NEXT)
    assert r.exit_code == 0, r.stdout
    assert f'vecten_convention = "{old}"' in table.read_text()
    assert f'name = "other-package"\nversion = "{old}"' in (repo / "uv.lock").read_text()


@pytest.mark.parametrize(
    ("rel", "stray"),
    [
        (
            "packages/vecten-shared/src/vecten_shared/versions.toml",
            '\n[stray]\nmcp = "ghcr.io/vecten/vecten-mcp:{old}"\n',
        ),
        ("packages/vecten/pyproject.toml", '\n[tool.stray]\npin = "vecten-mcp>={old}"\n'),
    ],
    ids=["table", "pyproject"],
)
def test_a_copy_of_the_version_a_bump_does_not_rewrite_is_refused(invoke, repo, rel, stray):
    with (repo / rel).open("a") as f:
        f.write(stray.format(old=VERSIONS.vecten))
    before = snapshot(repo)
    r = bump(invoke, repo, NEXT)
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert rel in json.loads(r.stdout)["error"]
    assert snapshot(repo) == before


@pytest.mark.skipif(
    not (REPO / "docs/releasing.md").is_file(),
    reason="the release procedure stays in the internal repository",
)
def test_releasing_names_the_command():
    text = (REPO / "docs/releasing.md").read_text()
    assert re.search(r"vecten dev bump-version <version>", text)
