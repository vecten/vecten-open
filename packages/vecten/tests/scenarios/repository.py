"""A project repository with real git history, and the GitHub that holds its pull requests.

`ScenarioGh` answers the `gh api` reads that shipping makes for reviewed gap fixes from the
pull requests `FixtureRepository.merge` recorded, so a ship sees the same review evidence as
it would on GitHub.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ScenarioGh:
    def __init__(self, repository: str):
        self.repository = repository
        self.pulls: dict[int, dict] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.calls: list[list[str]] = []
        # (commit, `-f` fields) of every commit status a ship set.
        self.statuses: list[tuple[str, list[str]]] = []

    def which(self) -> str:
        return "/usr/bin/gh"

    def _answer(self, args, body) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    def run(self, args, cwd, input=None) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if args[:1] != ["api"]:
            return subprocess.CompletedProcess(args, 1, "", "gh: not answered by the scenario")
        prefix = f"repos/{self.repository}"
        if args[1:3] == ["-X", "POST"] and args[3].startswith(prefix + "/statuses/"):
            self.statuses.append((args[3].rsplit("/", 1)[-1], args[4:]))
            return self._answer(args, {})
        endpoint = args[1]
        if endpoint == prefix:
            return self._answer(args, {"full_name": self.repository})
        if endpoint.startswith("search/issues?"):
            query = parse_qs(urlparse(endpoint).query)["q"][0]
            gap = json.loads(query.split(" in:body ", 1)[1])
            items = [{"number": n} for n, pull in self.pulls.items() if gap in pull["body"]]
            page = {"total_count": len(items), "incomplete_results": False, "items": items}
            return self._answer(args, [page])
        if endpoint.startswith(prefix + "/pulls/"):
            rest = endpoint[len(prefix + "/pulls/") :]
            number = int(rest.split("/")[0])
            if "/reviews" in rest:
                return self._answer(args, [self.reviews[number]])
            return self._answer(args, self.pulls[number])
        return subprocess.CompletedProcess(args, 1, "", f"gh: no answer for {endpoint}")

    def dispatched(self) -> list[list[str]]:
        """Every call that would start a workflow run."""
        return [c for c in self.calls if c[:2] == ["workflow", "run"]]


class FixtureRepository:
    def __init__(self, root: Path, repository: str):
        self.root = root
        self.gh = ScenarioGh(repository)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def init(self) -> str:
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Scenario")
        self.git("config", "user.email", "scenario@acme.test")
        return self.commit("Initial project")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str) -> None:
        self.git("checkout", "-q", "-b", name)

    def merge(self, branch: str, *, number: int, author: str, reviewer: str, body: str) -> str:
        """Merge `branch` into main as GitHub merges an approved pull request; the merge commit."""
        head = self.git("rev-parse", branch)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{number}")
        merge = self.git("rev-parse", "HEAD")
        merged_at = datetime.now(UTC)
        self.gh.pulls[number] = {
            "number": number,
            "body": body,
            "merged_at": _iso(merged_at),
            "merge_commit_sha": merge,
            "head": {"ref": branch, "sha": head},
            "base": {"repo": {"full_name": self.gh.repository}},
            "user": {"login": author},
        }
        self.gh.reviews[number] = [
            {
                "state": "APPROVED",
                "user": {"login": reviewer},
                "commit_id": head,
                "submitted_at": _iso(merged_at - timedelta(minutes=5)),
            }
        ]
        return merge
