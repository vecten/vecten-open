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


def merge(store, number, head, body="", commit="1" * 40, base="main"):
    with store.edit("acme/data") as repo:
        repo["pulls"][str(number)] = {
            "number": number,
            "title": "fix",
            "body": body,
            "head": head,
            "head_sha": "0" * 40,
            "base": base,
            "author": "acme-dev",
            "state": "merged",
            "created_at": "2026-10-22T00:00:00Z",
            "merged_at": "2026-10-22T00:01:00Z",
            "merge_commit_sha": commit,
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


def test_only_a_merged_fix_for_one_issue_on_main_clears_it(shell, tmp_path):
    make, store = shell
    env = make("three-failures")
    ids = [i["id"] for i in fixture("three-failures")["issues"]["issues"]]
    root = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(store.bare("acme/data")), str(root)], check=True)

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=fixture@example.org", *args],
            cwd=root,
            text=True,
        ).strip()

    def commit(path, text):
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        git("add", ".")
        git("commit", "-qm", "fixture change")
        git("push", "-q", "origin", "HEAD:refs/heads/main")
        return git("rev-parse", "HEAD")

    def listed():
        return [
            i["id"]
            for i in json.loads(vecten(env, "issues", "--env", "prod", "--json")[1])["issues"]
        ]

    connector = "connectors/hubspot/connector.py"
    model = "dbt/models/marts/schema.yml"
    baseline = commit(
        connector,
        "def deals():\n    for row in response:\n"
        "        yield {'dealname': row['properties'].get('dealname')}\n",
    )
    merge(store, 1, ids[0], commit=baseline)
    assert listed() == ids
    right = commit(
        connector,
        "def deals():\n    for row in response:\n"
        "        yield {'dealname': row['properties']['deal_name']}\n",
    )
    merge(store, 2, ids[0], commit=right, base="intermediate")
    merge(store, 3, ids[0], body=ids[1], commit=right)
    assert listed() == ids
    merge(store, 4, ids[0], commit=right)
    assert listed() == ids[1:]
    start = "models: [{name: sales_pipeline, columns: [{name: amount, data_tests: "
    broken = commit(model, start + "[]}]}]")
    merge(store, 5, ids[1], commit=broken)
    assert listed() == ids[1:]
    fixed = commit(
        model, start + """[{not_null: {config: {where: "stage in ('won', 'lost')"}}}]}]}]"""
    )
    merge(store, 6, ids[1], commit=fixed)
    assert listed() == ids[2:]
    merge(store, 7, "semantic/fix", body="Fixes gap-0123456789abcdef01234567.")
    assert listed() == ids[2:]
    metric = {
        "name": "total_revenue",
        "expression": {
            "dialects": [
                {
                    "dialect": "ANSI_SQL",
                    "expression": (
                        "SUM(sales_pipeline.amount) - SUM(sales_pipeline.refunded_amount)"
                    ),
                }
            ]
        },
    }
    semantic = commit("osi/metrics.json", json.dumps({"semantic_model": [{"metrics": [metric]}]}))
    merge(store, 8, "semantic/fix", body="Fixes gap-0123456789abcdef01234567.", commit=semantic)
    assert not listed()


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
