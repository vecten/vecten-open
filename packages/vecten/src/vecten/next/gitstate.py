"""The local git state the deploy prerequisites read, from the `git` binary alone."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitState:
    has_repo: bool
    has_origin: bool
    dirty: bool
    ahead: bool


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None


def read_git_state(root: Path) -> GitState:
    """Never raises: a missing `git` binary reads as no repository."""
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    # A project nested in another repository has none of its own: `github init` must create it.
    if (
        inside is None
        or inside.returncode != 0
        or inside.stdout.strip() != "true"
        or toplevel is None
        or toplevel.returncode != 0
        or Path(toplevel.stdout.strip()).resolve() != root.resolve()
    ):
        return GitState(has_repo=False, has_origin=False, dirty=False, ahead=False)
    origin = _git(root, "remote", "get-url", "origin")
    has_origin = origin is not None and origin.returncode == 0
    # A plain `status` refreshes the index and takes its lock, which `next` must never write.
    status = _git(root, "--no-optional-locks", "status", "--porcelain")
    dirty = status is not None and status.returncode == 0 and bool(status.stdout.strip())
    count = _git(root, "rev-list", "--count", "@{upstream}..HEAD")
    if count is None or count.returncode != 0 or not count.stdout.strip().isdigit():
        # With no upstream nothing is known to be pushed, so an origin means a push is due.
        ahead = has_origin
    else:
        ahead = int(count.stdout.strip()) > 0
    return GitState(has_repo=True, has_origin=has_origin, dirty=dirty, ahead=ahead)
