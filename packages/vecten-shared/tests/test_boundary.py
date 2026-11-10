import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PACKAGES = REPO / "packages"

FORBIDDEN = {
    "vecten_platform": {"vecten_mcp", "vecten"},
    "vecten_mcp": {"vecten_platform", "vecten"},
    "vecten_shared": {"vecten_platform", "vecten_mcp", "vecten"},
}


def imported_top_levels(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def violations(src_root: Path, forbidden: set[str]) -> list[str]:
    found = []
    for py in src_root.rglob("*.py"):
        hit = imported_top_levels(py) & forbidden
        if hit:
            shown = py.relative_to(REPO) if py.is_relative_to(REPO) else py
            found.append(f"{shown} imports {sorted(hit)}")
    return found


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_package_respects_import_boundary(package):
    dist = package.replace("_", "-")
    src = PACKAGES / dist / "src" / package
    assert src.is_dir(), f"missing {src}"
    assert violations(src, FORBIDDEN[package]) == []


def test_boundary_check_catches_a_planted_cross_import(tmp_path):
    bad = tmp_path / "vecten_platform" / "leak.py"
    bad.parent.mkdir()
    bad.write_text("import vecten_mcp\n")
    found = violations(tmp_path / "vecten_platform", FORBIDDEN["vecten_platform"])
    assert len(found) == 1 and found[0].endswith("imports ['vecten_mcp']")


def test_all_four_distributions_share_one_version():
    versions = set()
    for dist in ("vecten", "vecten-platform", "vecten-mcp", "vecten-shared"):
        text = (PACKAGES / dist / "pyproject.toml").read_text()
        line = next(line for line in text.splitlines() if line.startswith("version = "))
        versions.add(line)
    assert len(versions) == 1
