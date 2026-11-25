import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/close_issues.py"
WORKFLOW = yaml.safe_load((REPO / ".github/workflows/close-issues.yml").read_text())


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
        ("Summary first.\n\nFixes #12\n\nMore text.", [12]),
        ("Fixes #12\nCloses #12", [12]),
        (None, []),
        ("", []),
    ],
)
def test_a_line_of_its_own_declares_the_fix(body, issues):
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
        "    Fixes #190",
        "```\nFixes #190\n```",
        "~~~text\nCloses #5\n~~~",
        "````\n```\nFixes #190\n```\n````",
        "```\nFixes #190\n~~~\nCloses #5\n```",
        "<!-- Fixes #190 -->",
        "<!--\nFixes #190\n-->",
        "Ran it on `Fixes #190`. Fine.",
        'The issue said "Fixes #3".',
        "e.g.fixes #4",
    ],
)
def test_a_mention_is_not_a_declaration(body):
    assert close_issues.declared(body) == []


def test_a_declaration_after_a_closed_fence_counts():
    assert close_issues.declared("```\nFixes #1\n```\nFixes #2") == [2]


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "error", {}, io.BytesIO(b"{}"))


class Api:
    def __init__(self, issues, pulls=()):
        self.issues = issues
        self.pulls = list(pulls)
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("/pulls"):
            return self.pulls
        issue = int(path.split("/issues/")[1].split("/")[0])
        state = self.issues[issue]
        if isinstance(state, int):
            raise http_error(state)
        if method == "GET":
            return state
        return {}

    def closed(self):
        return [int(path.split("/")[-1]) for method, path, _ in self.calls if method == "PATCH"]


def pull(number, body, commit="abc", base="release/0.1.2", merged=True):
    return {
        "number": number,
        "body": body,
        "merge_commit_sha": commit,
        "base": {"ref": base},
        "merged_at": "2026-11-25T00:00:00Z" if merged else None,
    }


OPEN = {"state": "open"}


def run(monkeypatch, api, capsys):
    monkeypatch.setattr(close_issues, "call", api)
    code = close_issues.main("vecten/vecten-open", "release/0.1.2", ["abc"])
    return code, capsys.readouterr().out


def test_a_missing_number_is_reported_and_the_next_issue_still_closes(monkeypatch, capsys):
    api = Api({999: 404, 12: OPEN}, [pull(200, "Fixes #999\nFixes #12")])
    code, out = run(monkeypatch, api, capsys)
    assert code == 0
    assert api.closed() == [12]
    assert "::warning::#200 declares it fixes #999" in out
    assert (
        "POST",
        "/repos/vecten/vecten-open/issues/12/comments",
        {"body": "Fixed by #200, merged into release/0.1.2."},
    ) in api.calls


def test_a_permission_failure_fails_the_run_after_the_others_are_tried(monkeypatch, capsys):
    api = Api({11: 403, 12: OPEN}, [pull(200, "Fixes #11\nFixes #12")])
    code, out = run(monkeypatch, api, capsys)
    assert code == 1
    assert api.closed() == [12]
    assert "::error::closing #11 for #200 failed" in out


def test_pull_requests_and_closed_issues_are_left_alone(monkeypatch, capsys):
    api = Api(
        {7: {"state": "open", "pull_request": {}}, 8: {"state": "closed"}},
        [pull(200, "Fixes #7\nFixes #8")],
    )
    code, _ = run(monkeypatch, api, capsys)
    assert code == 0
    assert api.closed() == []


def test_only_the_pull_request_this_commit_merged_counts(monkeypatch, capsys):
    pulls = [
        pull(201, "Fixes #1", merged=False),
        pull(202, "Fixes #2", commit="other"),
        pull(203, "Fixes #3", base="main"),
        pull(204, "Fixes #4"),
    ]
    api = Api({1: OPEN, 2: OPEN, 3: OPEN, 4: OPEN}, pulls)
    code, _ = run(monkeypatch, api, capsys)
    assert code == 0
    assert api.closed() == [4]


def test_a_push_without_a_merged_pull_request_closes_nothing(monkeypatch):
    api = Api({})
    monkeypatch.setattr(close_issues, "call", api)
    monkeypatch.setattr(close_issues.time, "sleep", lambda _: None)
    assert close_issues.main("vecten/vecten-open", "release/0.1.2", ["abc"]) == 0
    assert [method for method, _, _ in api.calls] == ["GET"] * 3


def test_the_workflow_runs_on_pushes_to_release_branches_with_least_privilege():
    trigger = WORKFLOW[True]
    assert trigger == {"push": {"branches": ["release/**"]}}
    assert WORKFLOW["permissions"] == {}
    job = WORKFLOW["jobs"]["close-issues"]
    assert job["permissions"] == {"contents": "read", "issues": "write", "pull-requests": "read"}
    assert "scripts/close_issues.py" in job["steps"][-1]["run"]
