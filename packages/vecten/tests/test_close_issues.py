import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/close_issues.py"
WORKFLOW = yaml.safe_load((REPO / ".github/workflows/close-issues.yml").read_text())
BRANCH = "release/0.1.2"


def load():
    spec = importlib.util.spec_from_file_location("close_issues", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


close_issues = load()


@pytest.mark.parametrize(
    ("body", "issues"),
    [
        ("Fixes #190", [190]),
        ("fixes #190.", [190]),
        ("Closes: #5", [5]),
        ("- Resolves #3", [3]),
        ("1. Fixes #3", [3]),
        ("* closed #3, fixed #4 and resolves #5", [3, 4, 5]),
        ("Fixes #136. Fixes #137.", [136, 137]),
        ("Closes #116 and #117.", [116, 117]),
        ("Fixes #190. Then the tests ran.", [190]),
        ("Closes #126 through the documented option.", [126]),
        ("A grant could go undetected. Fixes #92.", [92]),
        ("Fixes #1, see #2", [1]),
        ("Fixes #1. #2 was split out.", [1]),
        ("Fixes #1, #2", [1, 2]),
        ("Fixes #1; closes #2", [1, 2]),
        ("**Fixes #7**", [7]),
        ("Summary first.\n\nFixes #12\n\nMore text.", [12]),
        ("A paragraph that wraps\nFixes #12 on its second line.", [12]),
        ("Fixes #12\nCloses #12", [12]),
        ("```\nFixes #1\n```\nFixes #2", [2]),
        ("> Quoted.\n\nFixes #2", [2]),
        (None, []),
        ("", []),
    ],
)
def test_a_declaration_opens_a_line_or_a_sentence(body, issues):
    assert close_issues.declared(body) == issues


@pytest.mark.parametrize(
    "body",
    [
        "Ran the parsing on `Fixes #190`, `Closes: #5` and `resolved #3`.",
        "- `Fixes #190`",
        "This fixes #190 in passing.",
        "#12 and fixes #13",
        "Refs #9",
        "Part of #142",
        "Addresses #131",
        "prefixes #7",
        "Fixes #",
        "> Fixes #190",
        ">Closes #5",
        # A line after a quote without a blank line continues the quote (lazy continuation).
        "> Original checklist:\nFixes #190",
        "> - item\n>\n> Closes #5",
        # A code span may run over a line break.
        "Example: `see\nFixes #190`",
        "``\nFixes #190 ``",
        "    Fixes #190",
        "```\nFixes #190\n```",
        "~~~text\nCloses #5\n~~~",
        "````\n```\nFixes #190\n```\n````",
        "```\nFixes #190\n~~~\nCloses #5\n```",
        "<!-- Fixes #190 -->",
        "<!--\nFixes #190\n-->",
        "# Fixes #190",
        "Ran it on `Fixes #190`. Fine.",
        'The issue said "Fixes #3".',
        "e.g.fixes #4",
    ],
)
def test_a_mention_is_not_a_declaration(body):
    assert close_issues.declared(body) == []


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "error", {}, io.BytesIO(b"{}"))


def pull(number, body, commit, base=BRANCH, merged=True):
    return {
        "number": number,
        "body": body,
        "merge_commit_sha": commit,
        "base": {"ref": base},
        "merged_at": "2026-11-25T00:00:00Z" if merged else None,
    }


class Api:
    """GitHub over a commit graph: `parents` maps a commit to its parents, first parent first."""

    def __init__(self, issues=None, parents=None, pulls=None):
        self.issues = issues or {}
        self.parents = parents or {}
        self.pulls = pulls or {}
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if "/commits/" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            if path.endswith("/pulls"):
                return self.pulls.get(sha, [])
            return {"parents": [{"sha": parent} for parent in self.parents[sha]]}
        issue = int(path.split("/issues/")[1].split("/")[0])
        state = self.issues[issue]
        if isinstance(state, int):
            raise http_error(state)
        return state if method == "GET" else {}

    def closed(self):
        return [int(path.split("/")[-1]) for method, path, _ in self.calls if method == "PATCH"]

    def looked_up(self):
        return [
            path.split("/")[-1]
            for method, path, _ in self.calls
            if "/issues/" in path and method == "GET"
        ]

    def pull_lookups(self):
        return [path.split("/")[-2] for _, path, _ in self.calls if path.endswith("/pulls")]


OPEN = {"state": "open"}


def run(monkeypatch, capsys, api, before="base", after="m1"):
    monkeypatch.setattr(close_issues, "call", api)
    monkeypatch.setattr(close_issues.time, "sleep", lambda _: None)
    code = close_issues.main("vecten/vecten-open", BRANCH, before, after)
    return code, capsys.readouterr().out


def squash(body, issues):
    return Api(issues, {"m1": ["base"]}, {"m1": [pull(200, body, "m1")]})


def test_a_missing_number_is_reported_and_the_next_issue_still_closes(monkeypatch, capsys):
    api = squash("Fixes #11\nFixes #12", {11: 404, 12: OPEN})
    code, out = run(monkeypatch, capsys, api)
    assert code == 0
    assert api.looked_up() == ["11", "12"]
    assert api.closed() == [12]
    assert "::warning::#200 declares it fixes #11" in out
    assert (
        "POST",
        "/repos/vecten/vecten-open/issues/12/comments",
        {"body": f"Fixed by #200, merged into {BRANCH}."},
    ) in api.calls


def test_a_permission_failure_fails_the_run_after_the_others_are_tried(monkeypatch, capsys):
    api = squash("Fixes #11\nFixes #12", {11: 403, 12: OPEN})
    code, out = run(monkeypatch, capsys, api)
    assert code == 1
    assert api.looked_up() == ["11", "12"]
    assert api.closed() == [12]
    assert "::error::closing #11 for #200 failed" in out


def test_pull_requests_and_closed_issues_are_left_alone(monkeypatch, capsys):
    api = squash(
        "Fixes #7\nFixes #8", {7: {"state": "open", "pull_request": {}}, 8: {"state": "closed"}}
    )
    code, _ = run(monkeypatch, capsys, api)
    assert code == 0
    assert api.closed() == []


def test_only_the_pull_request_this_commit_merged_counts(monkeypatch, capsys):
    pulls = [
        pull(201, "Fixes #1", "m1", merged=False),
        pull(202, "Fixes #2", "other"),
        pull(203, "Fixes #3", "m1", base="main"),
        pull(204, "Fixes #4", "m1"),
    ]
    api = Api({1: OPEN, 2: OPEN, 3: OPEN, 4: OPEN}, {"m1": ["base"]}, {"m1": pulls})
    code, _ = run(monkeypatch, capsys, api)
    assert code == 0
    assert api.closed() == [4]


def test_a_merge_commit_looks_up_only_itself_not_the_commits_it_brings(monkeypatch, capsys):
    # m1 merges the pull request's thirty commits c1..c30, which the push also delivers.
    parents = {"m1": ["base", "c30"]} | {f"c{i}": [f"c{i - 1}"] for i in range(2, 31)}
    parents["c1"] = ["base"]
    api = Api({5: OPEN}, parents, {"m1": [pull(200, "Fixes #5", "m1")]})
    code, _ = run(monkeypatch, capsys, api)
    assert code == 0
    assert api.pull_lookups() == ["m1"]
    assert api.closed() == [5]


def test_several_squash_merges_in_one_push_each_close_their_issues(monkeypatch, capsys):
    api = Api(
        {5: OPEN, 6: OPEN},
        {"m2": ["m1"], "m1": ["base"]},
        {"m1": [pull(200, "Fixes #5", "m1")], "m2": [pull(201, "Fixes #6", "m2")]},
    )
    code, _ = run(monkeypatch, capsys, api, after="m2")
    assert code == 0
    assert api.pull_lookups() == ["m1", "m2"]
    assert api.closed() == [5, 6]


def test_a_new_branch_landed_nothing(monkeypatch, capsys):
    api = Api()
    code, _ = run(monkeypatch, capsys, api, before="0" * 40)
    assert code == 0
    assert api.calls == []


def test_a_push_without_a_merged_pull_request_closes_nothing(monkeypatch, capsys):
    api = Api(parents={"m1": ["base"]})
    code, _ = run(monkeypatch, capsys, api)
    assert code == 0
    assert api.pull_lookups() == ["m1"] * 3


def test_a_before_outside_the_first_parent_line_fails(monkeypatch, capsys):
    api = Api(parents={"m1": ["root"], "root": []})
    with pytest.raises(RuntimeError, match="not a first parent"):
        run(monkeypatch, capsys, api, before="elsewhere")


def test_the_workflow_runs_the_locked_script_on_release_pushes_with_least_privilege():
    assert WORKFLOW[True] == {"push": {"branches": ["release/**"]}}
    assert WORKFLOW["permissions"] == {}
    job = WORKFLOW["jobs"]["close-issues"]
    assert job["permissions"] == {"contents": "read", "issues": "write", "pull-requests": "read"}
    step = job["steps"][-1]
    assert step["run"].startswith("uv run --locked --script scripts/close_issues.py")
    assert step["env"]["BEFORE"] == "${{ github.event.before }}"
    assert step["env"]["AFTER"] == "${{ github.event.after }}"
    assert (REPO / "scripts/close_issues.py.lock").is_file()
