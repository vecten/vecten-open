"""The evaluation harness's `vecten issues` replay for the `operate` cases: the fixture's list
and evidence through the real command's output contract, everything else passed through."""

import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import gh_standin, issues_replay  # noqa: E402

FILES = resources.files("vecten.knowhow").joinpath("skills/operate/evals/grader")


def fixture(name):
    return json.loads(FILES.joinpath(f"{name}.json").read_text())


@pytest.fixture
def shell(tmp_path):
    gh_dir = tmp_path / "github"
    gh_standin.Store(gh_dir).create("acme/data", private=True)

    def make(name):
        env = {**os.environ}
        env.update(
            issues_replay.install(
                tmp_path / "bin",
                fixture=FILES.joinpath(f"{name}.json"),
                gh_dir=gh_dir,
                python=sys.executable,
                environ=env,
            )
        )
        return env

    return make, gh_standin.Store(gh_dir)


def vecten(env, *args, cwd=None):
    result = subprocess.run(["vecten", *args], env=env, cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def merge(store, number, head, body=""):
    with store.edit("acme/data") as repo:
        repo["pulls"][str(number)] = {
            "number": number,
            "title": "fix",
            "body": body,
            "head": head,
            "head_sha": "0" * 40,
            "base": "main",
            "author": "acme-dev",
            "state": "merged",
            "created_at": "2026-10-22T00:00:00Z",
            "merged_at": "2026-10-22T00:01:00Z",
            "merge_commit_sha": "1" * 40,
        }


def test_the_list_is_the_fixture_in_the_commands_json_shape(shell):
    make, _ = shell
    code, out, _ = vecten(make("three-failures"), "issues", "--env", "prod", "--json")
    assert code == 0
    assert json.loads(out) == {**fixture("three-failures")["issues"], "ok": True}


def test_show_prints_the_issue_with_its_full_evidence(shell):
    make, _ = shell
    doc = fixture("three-failures")
    issue = doc["issues"]["issues"][0]
    env = make("three-failures")
    code, out, _ = vecten(env, "issues", "show", issue["id"], "--env", "prod", "--json")
    assert code == 0
    shown = json.loads(out)
    assert shown["env"] == "prod" and shown["ok"] is True
    assert shown["issue"]["evidence"] == {**issue["evidence"], **doc["show"][issue["id"]]}
    code, out, _ = vecten(env, "issues", "show", "iss_nope", "--env", "prod", "--json")
    assert code == 4 and "not open in prod" in json.loads(out)["error"]


def test_the_text_output_names_each_issue_and_how_to_read_one(shell):
    make, _ = shell
    code, out, _ = vecten(make("three-failures"), "issues", "--env", "prod")
    assert code == 0
    assert "[skill:connector]" in out and "vecten issues show <id>" in out


def test_an_issue_leaves_the_list_once_a_merged_pull_request_names_it(shell):
    make, store = shell
    env = make("three-failures")
    ids = [i["id"] for i in fixture("three-failures")["issues"]["issues"]]
    merge(store, 1, f"operate/{ids[0]}")
    listed = json.loads(vecten(env, "issues", "--env", "prod", "--json")[1])
    assert [i["id"] for i in listed["issues"]] == ids[1:]
    merge(store, 2, f"operate/{ids[1]}")
    merge(store, 3, "semantic/fix", body="Fixes gap-0123456789abcdef01234567.")
    listed = json.loads(vecten(env, "issues", "--env", "prod", "--json")[1])
    assert listed == {**fixture("three-failures")["after_merges"], "ok": True}


def test_an_unreadable_source_fails_the_list_with_its_details(shell):
    make, _ = shell
    code, out, _ = vecten(make("injected-instruction"), "issues", "--env", "prod", "--json")
    doc = json.loads(out)
    assert code == 5 and doc["ok"] is False and "gaps" in doc["error"]
    assert doc["issues"] == fixture("injected-instruction")["issues"]["issues"]


def test_every_other_command_reaches_the_real_vecten(shell, tmp_path):
    make, _ = shell
    code, out, _ = vecten(make("revoked-key"), "version", "--json")
    assert code == 0 and "vecten" in json.loads(out)
    code, out, _ = vecten(make("revoked-key"), "issues", "--json", cwd=tmp_path)
    assert code != 0 and "project" in json.loads(out)["error"]
