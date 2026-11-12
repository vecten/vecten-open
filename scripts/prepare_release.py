#!/usr/bin/env python3
"""Prepare an offline, history-free public tree. Never creates a repository or publishes."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

PACKAGES = ("vecten", "vecten-platform", "vecten-mcp", "vecten-shared")
ROOT_FILES = {
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "PRIVACY.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".gitignore",
    ".dockerignore",
    "conftest.py",
    "scripts/prepare_release.py",
    "scripts/check_extension_wheels.py",
}
PUBLIC_DIRS = {"packages", "terraform", "images", "plugins", ".claude-plugin", ".github"}
# Listed one by one, so a doc added under docs/ stays internal until it is named here.
PUBLIC_DOCS = {
    "docs/GLOSSARY.md",
    "docs/README.md",
    "docs/images/vecten-open-dark.svg",
    "docs/images/vecten-open-light.svg",
    "docs/agent-guide.md",
    "docs/agentic-flow.md",
    "docs/cli.md",
    "docs/connectors.md",
    "docs/deploy-aws.md",
    "docs/deploy-scaleway.md",
    "docs/mcp.md",
    "docs/orchestration.md",
    "docs/warehouse.md",
    "docs/why-vecten.md",
    "docs/admin-panel.md",
    "docs/exit-codes.md",
    "docs/extensions.md",
    "docs/extensions-private-git.md",
    "docs/feedback-loop.md",
    "docs/mcp-audit.md",
    "docs/mcp-identity.md",
    "docs/mcp-tools.md",
    "docs/operations.md",
    "docs/query-metrics.md",
    "docs/semantic-model.md",
    "docs/skill-evaluations.md",
}
PUBLIC_AGENTS = (
    "# Vecten Open: working in this repository\n"
    "\n"
    "Four Python distributions released together: vecten (the CLI), vecten-platform (ingestion\n"
    "and modelling runtime), vecten-mcp (the warehouse server) and vecten-shared (runtime\n"
    "contracts). Python 3.14 and uv.\n"
    "\n"
    "## Commands\n"
    "\n"
    "- Setup: `uv sync --locked --all-packages`\n"
    "- Lint: `uv run ruff check .` and `uv run ruff format --check .`\n"
    '- Unit tests: `uv run pytest -m "not docker and not network and not terraform and not '
    'browser and not snowflake_live" -n auto --dist loadgroup`\n'
    "- Docker, network, Terraform and browser tiers run in CI; run one locally with its marker.\n"
    "- After editing a skill under packages/vecten/src/vecten/knowhow/skills/:\n"
    "  `uv run vecten dev render-plugin --repo .`, then commit the rendered plugin and\n"
    "  docs/agent-guide.md.\n"
    "\n"
    "## Rules\n"
    "\n"
    "- Runtime packages import independently of the CLI and of each other.\n"
    "- Package versions, image tags and Terraform refs stay consistent with\n"
    "  packages/vecten-shared/src/vecten_shared/versions.toml; `vecten dev bump-version`\n"
    "  changes them.\n"
    "- packages/vecten/src/vecten/deploy/vm/apply.py runs on the VM's system Python 3.9 with\n"
    "  the standard library only; terraform/scaleway/modules/host/agent/apply.py is its\n"
    "  byte-identical copy.\n"
    "- Requirements live in .ai/openspec/specs/ (one folder per capability); a behaviour\n"
    "  change starts there. The glossary is docs/GLOSSARY.md.\n"
    "- A code comment states the constraint that makes the code correct, never the change\n"
    "  history.\n"
    "- Branch from the open release/<version> branch and open pull requests against it; main\n"
    "  takes only release/ and hotfix/ merges. CONTRIBUTING.md has the branch and release rules.\n"
    "- Never commit secrets. The maintainer merges; deployments and publication need explicit\n"
    "  approval.\n"
)
PUBLIC_CLAUDE = "@AGENTS.md\n"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def public(name: str) -> bool:
    parts = PurePosixPath(name).parts
    if not parts or any(
        p in {".git", ".env", ".venv", ".vecten", "__pycache__", ".terraform"}
        or p.startswith(".env.")
        or p.endswith((".tfstate", ".tfstate.backup"))
        for p in parts
    ):
        return False
    return (
        name in ROOT_FILES
        or name in PUBLIC_DOCS
        or parts[0] in PUBLIC_DIRS
        or name.startswith(".ai/openspec/specs/")
    )


def prepare(root: Path, destination: Path) -> list[str]:
    root, destination = root.resolve(), destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination must not exist")
    if destination.resolve().is_relative_to(root):
        raise ValueError("destination must be outside the source checkout")
    if git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("source checkout must be clean, including untracked files")
    tracked = git(root, "ls-files", "-z").split("\0")
    names = sorted(name for name in tracked if public(name))
    if not any(n.startswith(".ai/openspec/specs/") and n.endswith("/spec.md") for n in names):
        raise ValueError(
            "live specs are absent; complete and archive/sync the release change first"
        )
    version = tomllib.loads(
        (root / "packages/vecten-shared/src/vecten_shared/versions.toml").read_text()
    )["vecten"]
    for package in PACKAGES:
        data = tomllib.loads((root / "packages" / package / "pyproject.toml").read_text())[
            "project"
        ]
        if data["name"] != package or data["version"] != version:
            raise ValueError(f"{package} does not match release {version}")
    for name in names:
        path = root / name
        if any(
            p.is_symlink() for p in [path, *path.parents] if p != root and p.is_relative_to(root)
        ):
            raise ValueError(f"export refuses symlink: {name}")
        if not path.is_file():
            raise ValueError(f"not a regular tracked file: {name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vecten-export-", dir=destination.parent) as tmp:
        staged = Path(tmp) / "public"
        staged.mkdir()
        for name in names:
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        readme = staged / "README.md"
        readme.write_text(readme.read_text().split("\n## Internal phase\n")[0].rstrip() + "\n")
        (staged / "AGENTS.md").write_text(PUBLIC_AGENTS)
        (staged / "CLAUDE.md").write_text(PUBLIC_CLAUDE)
        staged.rename(destination)
    return [*names, "AGENTS.md", "CLAUDE.md"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        files = prepare(args.source, args.destination)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"release preparation refused: {exc}\n")
    print(
        f"Prepared {len(files)} files in {args.destination}; "
        "review before initializing public history."
    )


if __name__ == "__main__":
    main()
