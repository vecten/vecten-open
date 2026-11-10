import json
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest
import yaml


class Github:
    def __init__(self, pulls, reviews):
        self.pulls, self.reviews = pulls, reviews
        self.calls = []

    def run(self, args, cwd):
        self.calls.append(args)
        endpoint = args[1]
        if "/reviews?" in endpoint:
            number = int(endpoint.split("/pulls/")[1].split("/")[0])
            data = [getattr(self, "reviews_for", {}).get(number, self.reviews)]
        elif endpoint.startswith("search/issues?"):
            data = [
                {
                    "total_count": len(self.pulls),
                    "incomplete_results": False,
                    "items": [{"number": p["number"]} for p in self.pulls],
                }
            ]
        elif "/pulls/" in endpoint:
            data = next(p for p in self.pulls if p["number"] == int(endpoint.rsplit("/", 1)[-1]))
        else:
            data = {"full_name": "example/project"}
        return CompletedProcess(args, 0, json.dumps(data), "")


def inputs(tmp_path):
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals/semantic.yaml").write_text(
        yaml.safe_dump(
            {
                "cases": [
                    {
                        "name": "revenue",
                        "gap": "gap-1",
                        "request": {"metrics": ["revenue"]},
                        "expect": {"outcome": "compiled", "sql_sha256": "fixture"},
                    },
                ]
            }
        )
    )
    config = SimpleNamespace(deploy=SimpleNamespace(github_repository="example/project"))
    pull = {
        "number": 1,
        "body": "Fixes gap-1",
        "head": {"ref": "semantic/gap-1", "sha": "d" * 40},
        "base": {"repo": {"full_name": "example/project"}},
        "merged_at": "2026-10-20T10:00:00Z",
        "merge_commit_sha": "b" * 40,
        "user": {"login": "author"},
    }
    review = {
        "user": {"login": "reviewer"},
        "state": "APPROVED",
        "commit_id": "d" * 40,
        "submitted_at": "2026-10-20T09:00:00Z",
    }
    return config, pull, review


def test_bundle_records_only_reviewed_merged_gap_fixes_in_ancestry(tmp_path):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, review = inputs(tmp_path)
    github = Github([pull], [review])
    result = feedback_manifest(tmp_path, config, "c" * 40, {"ancestors": ["b" * 40]}, gh=github)
    assert result["commit"] == "c" * 40
    assert result["resolutions"][0]["gap"] == "gap-1"
    assert result["resolutions"][0]["reviewers"] == ["reviewer"]
    assert result["resolutions"][0]["metrics"] == ["revenue"]
    assert all(call[0] == "api" for call in github.calls)


@pytest.mark.parametrize(
    "change",
    [
        {"merged_at": None},
        {"merge_commit_sha": "e" * 40},
        {"body": "Unrelated"},
        {"head": {"ref": "other", "sha": "d" * 40}},
        {"base": {"repo": {"full_name": "other/project"}}},
    ],
)
def test_bundle_refuses_unrelated_pr_as_verification(tmp_path, change):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, review = inputs(tmp_path)
    result = feedback_manifest(
        tmp_path,
        config,
        "c" * 40,
        {"ancestors": ["b" * 40]},
        gh=Github([{**pull, **change}], [review]),
    )
    assert result["resolutions"] == []


def test_superseded_approval_does_not_verify_definition(tmp_path):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, review = inputs(tmp_path)
    gh = Github([pull], [review, {**review, "state": "CHANGES_REQUESTED"}])
    assert (
        feedback_manifest(tmp_path, config, "b" * 40, {"ancestors": []}, gh=gh)["resolutions"] == []
    )


def test_no_gap_evals_requires_no_github_access(tmp_path):
    from vecten.deploy.feedback import feedback_manifest

    gh = Github([], [])
    result = feedback_manifest(tmp_path, None, "b" * 40, {"ancestors": []}, gh=gh)
    assert result["resolutions"] == [] and gh.calls == []


@pytest.mark.parametrize("configured", ["Example/Project", "example/old-name"])
def test_repository_alias_uses_canonical_github_identity(tmp_path, configured):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, review = inputs(tmp_path)
    config.deploy.github_repository = configured
    gh = Github([pull], [review])
    result = feedback_manifest(tmp_path, config, "b" * 40, {"ancestors": []}, gh=gh)
    assert result["resolutions"][0]["pull_request"] == "https://github.com/example/project/pull/1"


def test_missing_gh_is_a_safe_failure(tmp_path):
    from vecten.deploy.feedback import feedback_manifest
    from vecten_shared.output import Failure

    config, _, _ = inputs(tmp_path)

    class Missing:
        def run(self, *args):
            raise FileNotFoundError("sensitive environment path")

    with pytest.raises(Failure, match="GitHub CLI") as failure:
        feedback_manifest(tmp_path, config, "b" * 40, {"ancestors": []}, gh=Missing())
    assert "sensitive" not in str(failure.value)


def test_gap_lookup_does_not_paginate_closed_pr_history(tmp_path):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, review = inputs(tmp_path)
    gh = Github([pull], [review])
    feedback_manifest(tmp_path, config, "b" * 40, {"ancestors": []}, gh=gh)
    assert any(call[1].startswith("search/issues?") for call in gh.calls)
    assert not any("state=closed" in call[1] for call in gh.calls)


UNREVIEWED_LINE = (
    "gap gap-1: https://github.com/example/project/pull/1 merged without an approving review; "
    "it resolves the gap only once an admin confirms it in the panel"
)


@pytest.mark.parametrize(
    "reviews",
    [
        [],
        [{"user": {"login": "author"}, "state": "APPROVED", "commit_id": "d" * 40}],
        [
            {
                "user": {"login": "reviewer"},
                "state": "APPROVED",
                "commit_id": "d" * 40,
                "submitted_at": "2026-10-20T11:00:00Z",
            }
        ],
    ],
    ids=["no-review", "self-approval", "approved-after-merge"],
)
def test_unreviewed_merged_fix_is_listed_and_printed(tmp_path, capsys, reviews):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, _ = inputs(tmp_path)
    result = feedback_manifest(
        tmp_path, config, "c" * 40, {"ancestors": ["b" * 40]}, gh=Github([pull], reviews)
    )
    assert result["resolutions"] == []
    assert result["unreviewed"] == [
        {
            "gap": "gap-1",
            "pull_request": "https://github.com/example/project/pull/1",
            "merge_commit": "b" * 40,
            "author": "author",
            "merged_at": "2026-10-20T10:00:00Z",
            "metrics": ["revenue"],
        }
    ]
    assert capsys.readouterr().err.splitlines() == [UNREVIEWED_LINE]


def test_reviewed_fix_leaves_no_unreviewed_entry_for_its_gap(tmp_path, capsys):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, review = inputs(tmp_path)
    earlier = {
        **pull,
        "number": 2,
        "merged_at": "2026-10-19T10:00:00Z",
        "merge_commit_sha": "a" * 40,
    }
    gh = Github([pull, earlier], [review])
    gh.reviews_for = {2: []}
    result = feedback_manifest(
        tmp_path, config, "c" * 40, {"ancestors": ["b" * 40, "a" * 40]}, gh=gh
    )
    assert [r["pull_request"][-1] for r in result["resolutions"]] == ["1"]
    assert result["unreviewed"] == []
    assert capsys.readouterr().err == ""


def test_only_the_newest_unreviewed_merge_is_listed_for_a_gap(tmp_path):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, _ = inputs(tmp_path)
    earlier = {
        **pull,
        "number": 2,
        "merged_at": "2026-10-19T10:00:00Z",
        "merge_commit_sha": "a" * 40,
    }
    result = feedback_manifest(
        tmp_path,
        config,
        "c" * 40,
        {"ancestors": ["b" * 40, "a" * 40]},
        gh=Github([earlier, pull], []),
    )
    assert [entry["merge_commit"] for entry in result["unreviewed"]] == ["b" * 40]


def test_unrelated_pull_request_is_not_listed_as_unreviewed(tmp_path, capsys):
    from vecten.deploy.feedback import feedback_manifest

    config, pull, _ = inputs(tmp_path)
    gh = Github([{**pull, "head": {"ref": "other", "sha": "d" * 40}}], [])
    result = feedback_manifest(tmp_path, config, "c" * 40, {"ancestors": ["b" * 40]}, gh=gh)
    assert result["unreviewed"] == [] and capsys.readouterr().err == ""
