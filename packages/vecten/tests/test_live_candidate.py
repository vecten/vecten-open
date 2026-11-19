import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/live/candidate.py"
IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}
WRITE_VERSION = "import pathlib, sys; pathlib.Path('VERSION').write_text(sys.argv[1] + '\\n')"
EXPLODE = "import sys; print('bump exploded', file=sys.stderr); sys.exit(1)"
# A subprocess cannot be monkeypatched, so this stand-in swaps the constants and calls main.
WRAPPER = f"""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("candidate", {str(SCRIPT)!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.BUMP = json.loads(os.environ["TEST_BUMP"])
mod.LOCK = json.loads(os.environ["TEST_LOCK"])
sys.exit(mod.main(sys.argv[1:]))
"""


def mod():
    spec = importlib.util.spec_from_file_location("candidate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **IDENTITY},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(cwd, name, text, message):
    (cwd / name).write_text(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "switch", "-c", "main")
    commit(clone, "README.md", "base\n", "base")
    commit(clone, "VERSION", "0.1.1\n", "version")
    git(clone, "push", "origin", "main")
    git(clone, "push", "origin", "main:release/0.1.2")
    git(clone, "tag", "-a", "v0.1.1", "-m", "release")
    git(clone, "push", "origin", "v0.1.1")
    return clone


def cut(repo, *args, bump=None, bump_command=None, live="1"):
    wrapper = repo.parent / "wrapper.py"
    wrapper.write_text(WRAPPER)
    env = {
        **{key: value for key, value in os.environ.items() if key != "VECTEN_LIVE"},
        **IDENTITY,
        "TEST_BUMP": json.dumps(bump_command or [sys.executable, "-c", bump or WRITE_VERSION]),
        "TEST_LOCK": json.dumps([sys.executable, "-c", "pass"]),
        **({"VECTEN_LIVE": live} if live is not None else {}),
    }
    return subprocess.run(
        [sys.executable, str(wrapper), "cut", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def origin_of(repo):
    return repo.parent / "origin.git"


def remote_refs(repo):
    return git(repo, "ls-remote", "origin")


def test_first_candidate_is_rc1():
    assert mod().next_candidate(["v0.1.1", "v0.1.1rc2"], ["origin/rc/0.1.1rc2"], "0.1.2") == (
        "0.1.2rc1"
    )


def test_next_candidate_follows_the_highest_existing():
    assert mod().next_candidate(["v0.1.2rc1", "v0.1.2rc3"], [], "0.1.2") == "0.1.2rc4"


def test_next_candidate_counts_a_remote_branch_without_its_tag():
    m = mod()
    assert m.next_candidate(["v0.1.2rc1"], ["origin/rc/0.1.2rc2"], "0.1.2") == "0.1.2rc3"
    assert m.next_candidate([], ["origin/rc/0.1.2rc1", "origin/rc/0.1.3rc4"], "0.1.2") == (
        "0.1.2rc2"
    )


def test_a_branch_pushed_without_its_tag_spends_the_number(repo, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin_of(repo)), str(other))
    git(other, "push", "origin", "origin/main:refs/heads/rc/0.1.2rc1")
    result = cut(repo, "--version", "0.1.2")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["candidate"] == "0.1.2rc2"


@pytest.mark.parametrize("live", [None, "0", ""])
def test_cut_refuses_outside_the_wrapper(repo, live):
    before = remote_refs(repo)
    result = cut(repo, "--version", "0.1.2", live=live)
    assert result.returncode == 3
    assert result.stdout == ""
    assert len(result.stderr.strip().splitlines()) == 1
    assert "scripts/live/access.py run --github upstream" in result.stderr
    assert git(repo, "branch", "--list", "rc/*") == ""
    assert remote_refs(repo) == before


def test_a_dry_run_outside_the_wrapper_skips_the_fetch_and_says_so(repo, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin_of(repo)), str(other))
    git(other, "tag", "-a", "v0.1.2rc1", "-m", "rc", "origin/main")
    git(other, "push", "origin", "v0.1.2rc1")
    before = remote_refs(repo)
    result = cut(repo, "--version", "0.1.2", "--dry-run", live=None)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["candidate"] == "0.1.2rc1"
    assert "origin was not fetched" in result.stderr
    assert git(repo, "tag", "--list", "v0.1.2rc1") == "v0.1.2rc1"
    assert remote_refs(repo) == before


def test_a_tag_only_on_the_remote_is_counted(repo, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin_of(repo)), str(other))
    git(other, "tag", "-a", "v0.1.2rc1", "-m", "rc", "origin/main")
    git(other, "push", "origin", "v0.1.2rc1")
    result = cut(repo, "--version", "0.1.2")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["candidate"] == "0.1.2rc2"


def test_cut_pushes_the_branch_and_an_annotated_tag(repo):
    result = cut(repo, "--version", "0.1.2")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["candidate"] == "0.1.2rc1"
    assert out["branch"] == "rc/0.1.2rc1"
    assert out["tag"] == "v0.1.2rc1"
    assert out["pushed"] is True
    origin = origin_of(repo)
    assert git(origin, "cat-file", "-t", "v0.1.2rc1") == "tag"
    assert git(origin, "rev-parse", "rc/0.1.2rc1") == out["commit"]
    assert git(origin, "log", "-1", "--format=%s", "rc/0.1.2rc1") == "chore(release): 0.1.2rc1"
    assert git(origin, "show", "rc/0.1.2rc1:VERSION") == "0.1.2rc1"


def test_with_refs_are_merged_before_the_bump(repo):
    git(repo, "switch", "-c", "fix/thing")
    commit(repo, "fix.txt", "fix\n", "fix")
    fix = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    result = cut(repo, "--version", "0.1.2", "--with", "fix/thing")
    assert result.returncode == 0, result.stderr
    git(repo, "merge-base", "--is-ancestor", fix, "v0.1.2rc1")
    merge = git(repo, "log", "-2", "--format=%P", "v0.1.2rc1").splitlines()[1]
    assert len(merge.split()) == 2


def test_dry_run_pushes_nothing(repo):
    before = remote_refs(repo)
    result = cut(repo, "--version", "0.1.2", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pushed"] is False
    assert remote_refs(repo) == before
    assert git(repo, "tag", "--list", "v0.1.2rc1") == "v0.1.2rc1"
    assert git(repo, "branch", "--list", "rc/0.1.2rc1")


def test_a_dirty_tree_is_refused(repo):
    (repo / "README.md").write_text("changed\n")
    result = cut(repo, "--version", "0.1.2")
    assert result.returncode == 3
    assert len(result.stderr.strip().splitlines()) == 1
    assert git(repo, "branch", "--list", "rc/*") == ""


@pytest.mark.parametrize("version", ["0.1", "v0.1.2", "0.1.2rc1", "x.y.z"])
def test_a_malformed_version_is_refused(repo, version):
    result = cut(repo, "--version", version)
    assert result.returncode == 3
    assert result.stdout == ""


def test_a_missing_version_is_a_usage_error(repo):
    assert cut(repo).returncode == 2


def assert_cleaned_up(repo, before):
    assert git(repo, "branch", "--list", "rc/*") == ""
    assert git(repo, "tag", "--list", "v0.1.2rc*") == ""
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""
    assert remote_refs(repo) == before


def test_a_merge_conflict_leaves_no_branch_and_no_tag(repo):
    for name in ("a", "b"):
        git(repo, "switch", "-c", f"fix/{name}", "main")
        commit(repo, "README.md", f"{name}\n", name)
    git(repo, "switch", "main")
    before = remote_refs(repo)
    result = cut(repo, "--version", "0.1.2", "--with", "fix/a", "--with", "fix/b")
    assert result.returncode == 3
    assert len(result.stderr.strip().splitlines()) == 1
    assert_cleaned_up(repo, before)


def test_a_failed_bump_leaves_no_branch_and_no_tag(repo):
    before = remote_refs(repo)
    result = cut(repo, "--version", "0.1.2", bump=EXPLODE)
    assert result.returncode == 3
    assert "bump exploded" in result.stderr
    assert_cleaned_up(repo, before)


def test_a_failed_push_removes_what_it_created(repo):
    hook = origin_of(repo) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = remote_refs(repo)
    result = cut(repo, "--version", "0.1.2")
    assert result.returncode == 3
    assert_cleaned_up(repo, before)


def test_a_failed_tag_push_after_the_branch_push_says_the_branch_is_on_origin(repo):
    hook = origin_of(repo) / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        'while read old new ref; do case "$ref" in refs/tags/*) exit 1;; esac; done\n'
        "exit 0\n"
    )
    hook.chmod(0o755)
    result = cut(repo, "--version", "0.1.2")
    assert result.returncode == 3
    assert len(result.stderr.strip().splitlines()) == 1
    assert "branch rc/0.1.2rc1 is already on origin" in result.stderr
    assert git(repo, "branch", "--list", "rc/*") == ""
    assert git(repo, "tag", "--list", "v0.1.2rc*") == ""
    assert git(repo, "branch", "--show-current") == "main"
    refs = remote_refs(repo)
    assert "refs/heads/rc/0.1.2rc1" in refs
    assert "refs/tags/v0.1.2rc1" not in refs


def test_a_failed_cut_from_a_detached_head_returns_to_that_commit_detached(repo):
    git(repo, "switch", "--detach", "origin/release/0.1.2")
    start = git(repo, "rev-parse", "HEAD")
    result = cut(repo, "--version", "0.1.2", bump=EXPLODE)
    assert result.returncode == 3
    assert git(repo, "rev-parse", "HEAD") == start
    assert git(repo, "branch", "--show-current") == ""
    assert git(repo, "branch", "--list", "rc/*") == ""
    assert git(repo, "tag", "--list", "v0.1.2rc*") == ""


def test_a_cut_after_a_dry_run_is_the_next_candidate(repo):
    assert cut(repo, "--version", "0.1.2", "--dry-run").returncode == 0
    result = cut(repo, "--version", "0.1.2")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["candidate"] == "0.1.2rc2"


def test_a_local_only_unrelated_tag_survives_a_cut(repo):
    git(repo, "tag", "local-note")
    assert cut(repo, "--version", "0.1.2").returncode == 0
    assert git(repo, "tag", "--list", "local-note") == "local-note"


def test_a_missing_bump_executable_is_refused_and_cleaned_up(repo):
    before = remote_refs(repo)
    result = cut(repo, "--version", "0.1.2", bump_command=["/nonexistent/uv-bump"])
    assert result.returncode == 3
    assert len(result.stderr.strip().splitlines()) == 1
    assert_cleaned_up(repo, before)


def test_untracked_files_left_by_the_bump_are_not_committed(repo):
    bump = WRITE_VERSION + "; pathlib.Path('stray.txt').write_text('x')"
    result = cut(repo, "--version", "0.1.2", bump=bump)
    assert result.returncode == 0, result.stderr
    assert git(origin_of(repo), "ls-tree", "--name-only", "v0.1.2rc1").split() == [
        "README.md",
        "VERSION",
    ]
