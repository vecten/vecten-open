import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
UNIT_SELECTION = (
    '-m "not docker and not network and not terraform and not browser and not snowflake_live"'
)


def exporter():
    spec = importlib.util.spec_from_file_location(
        "prepare_release", REPO / "scripts/prepare_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root, *args):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


def source(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "README.md": "# Product\n\n## Internal phase\nPrivate decision\n",
        "CLAUDE.md": "Private decisions\n",
        "AGENTS.md": "Private decisions\n",
        "CONTRIBUTING.md": "# Contributing\n",
        "SECURITY.md": "# Security\n",
        ".github/ISSUE_TEMPLATE/bug.yml": "name: Bug\n",
        "LICENSE": "Apache License\n",
        "docs/GLOSSARY.md": "# Glossary\n",
        ".ai/openspec/specs/cli/example/spec.md": "# Public requirement\n",
        ".ai/openspec/changes/private/design.md": "private client",
        ".ai/research/private.md": "private client",
        ".env": "secret",
        "packages/vecten/src/vecten/app.py": "pass\n",
        "packages/vecten/.env": "nested secret",
        "docs/client.md": "private client",
    }
    for name in ("vecten", "vecten-platform", "vecten-mcp", "vecten-shared"):
        files[f"packages/{name}/pyproject.toml"] = f'[project]\nname="{name}"\nversion="0.1.0"\n'
    files["packages/vecten-shared/src/vecten_shared/versions.toml"] = 'vecten="0.1.0"\n'
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "init")
    git(root, "add", "--all")
    git(root, "commit", "-m", "fixture")
    return root


def test_export_has_no_internal_material_or_history(tmp_path):
    root = source(tmp_path)
    dest = tmp_path / "public"
    exporter().prepare(root, dest)
    assert (dest / "LICENSE").exists()
    assert (dest / ".ai/openspec/specs/cli/example/spec.md").exists()
    assert not (dest / ".git").exists()
    for rel in [
        ".ai/research",
        ".ai/openspec/changes",
        ".env",
        "packages/vecten/.env",
        "docs/client.md",
    ]:
        assert not (dest / rel).exists(), rel
    assert "Private" not in (dest / "README.md").read_text()
    assert (dest / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    agents = (dest / "AGENTS.md").read_text()
    assert "Private" not in agents and UNIT_SELECTION in agents
    assert len(agents.splitlines()) < 60
    for rel in ("CONTRIBUTING.md", "SECURITY.md", ".github/ISSUE_TEMPLATE/bug.yml"):
        assert (dest / rel).is_file(), rel
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=root) == b""


@pytest.mark.parametrize("issue", ["dirty", "versions", "specs", "destination", "symlink"])
def test_export_refuses_unready_input(tmp_path, issue):
    root = source(tmp_path)
    dest = tmp_path / "public"
    if issue == "dirty":
        (root / "README.md").write_text("changed")
    if issue == "versions":
        p = root / "packages/vecten/pyproject.toml"
        p.write_text(p.read_text().replace("0.1.0", "0.2.0"))
        git(root, "add", ".")
        git(root, "commit", "-m", "mismatch")
    if issue == "specs":
        git(root, "rm", "-r", ".ai/openspec/specs")
        git(root, "commit", "-m", "no live specs")
    if issue == "destination":
        dest.mkdir()
    if issue == "symlink":
        p = root / "packages/vecten/src/leak"
        p.symlink_to(root / ".env")
        git(root, "add", ".")
        git(root, "commit", "-m", "symlink")
    with pytest.raises(ValueError):
        exporter().prepare(root, dest)
    assert not dest.exists() or issue == "destination"


def test_workspace_dependencies_are_exact_release_pins():
    import tomllib

    from packaging.requirements import Requirement

    from vecten_shared.versions import VERSIONS

    for name in exporter().PACKAGES:
        p = tomllib.loads((REPO / "packages" / name / "pyproject.toml").read_text())["project"]
        for dependency in p.get("dependencies", []):
            req = Requirement(dependency)
            if req.name in exporter().PACKAGES:
                assert str(req.specifier) == f"=={VERSIONS.vecten}", (name, dependency)


def _doc_links(path, text):
    targets = re.findall(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)", text)
    return {
        (path.parent / target).resolve().relative_to(REPO).as_posix()
        for target in targets
        if "://" not in target
    }


def exported_markdown():
    release = exporter()
    readme = (REPO / "README.md").read_text().split("\n## Internal phase\n")[0]
    sources = {REPO / "README.md": readme}
    names = {n for n in release.ROOT_FILES | release.PUBLIC_DOCS if n.endswith(".md")}
    names |= {f"packages/{p}/README.md" for p in release.PACKAGES}
    names |= {".github/pull_request_template.md"}
    sources |= {REPO / n: (REPO / n).read_text() for n in names if n != "README.md"}
    return sources


def test_every_doc_the_public_files_link_to_is_exported():
    release = exporter()
    sources = exported_markdown()
    linked = set().union(*(_doc_links(path, text) for path, text in sources.items()))
    missing = sorted(
        target
        for target in linked
        if not target.startswith(".ai/openspec/specs/") and not release.public(target)
    )
    assert missing == []


def test_every_public_doc_exists():
    assert all((REPO / doc).is_file() for doc in exporter().PUBLIC_DOCS)


def test_every_docs_page_is_exported():
    pages = {p.relative_to(REPO).as_posix() for p in (REPO / "docs").glob("*.md")}
    assert pages - exporter().PUBLIC_DOCS <= {"docs/releasing.md"}


def test_every_root_file_the_tests_and_ci_need_is_exported():
    # The root conftest defines --shard, which CI passes, and closes the state stores whose
    # ResourceWarnings would otherwise fail the suite under filterwarnings = error.
    assert {"conftest.py", "pyproject.toml", "uv.lock"} <= exporter().ROOT_FILES


def test_contributing_pytest_selection_matches_agents_md():
    agents = exporter().PUBLIC_AGENTS
    assert UNIT_SELECTION in agents
    assert UNIT_SELECTION in (REPO / "CONTRIBUTING.md").read_text()
    assert len(agents.splitlines()) < 60


PUBLIC_URL = "https://github.com/vecten/vecten-open"


def test_package_metadata_identifies_the_project():
    import tomllib

    for name in exporter().PACKAGES:
        root = REPO / "packages" / name
        p = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        assert (root / p["readme"]).is_file(), name
        assert p["authors"] == [{"name": "Vecten"}], name
        assert p["keywords"] and all(" " not in k or k.islower() for k in p["keywords"]), name
        assert name != "vecten" or "cli" in p["keywords"]
        assert "Development Status :: 3 - Alpha" in p["classifiers"], name
        assert "Programming Language :: Python :: 3.14" in p["classifiers"], name
        for label in ("Homepage", "Source", "Documentation", "Issues", "Changelog", "Security"):
            assert p["urls"][label].startswith(PUBLIC_URL), (name, label)


def test_a_package_bundling_fonts_declares_their_licence():
    import tomllib

    for name in exporter().PACKAGES:
        root = REPO / "packages" / name
        fonts = [p for p in (root / "src").rglob("*") if p.suffix in (".ttf", ".otf", ".woff2")]
        p = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        if not fonts:
            assert p["license"] == "Apache-2.0", name
            continue
        assert p["license"] == "Apache-2.0 AND OFL-1.1", name
        notices = {(f.parent / "OFL.txt").relative_to(root).as_posix() for f in fonts}
        assert notices <= set(p["license-files"]), name
        assert all((root / n).is_file() for n in notices), name
        # An Apache-only classifier would understate a wheel that also carries OFL fonts.
        assert not any(c.startswith("License ::") for c in p["classifiers"]), name
