"""The workspace conftest's `--shard I/N`, which splits the unit suite across CI jobs."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_CONFTEST = Path(__file__).resolve().parents[3] / "conftest.py"

TESTS = """
import pytest

def test_a(): pass
def test_b(): pass
@pytest.mark.slow
def test_c(): pass
def test_d(): pass
@pytest.mark.xdist_group("shared")
def test_e(): pass
def test_f(): pass
@pytest.mark.xdist_group("shared")
def test_g(): pass
@pytest.mark.xdist_group(name="shared")
def test_h(): pass
def test_i(): pass
"""


@pytest.fixture
def suite(tmp_path):
    shutil.copyfile(ROOT_CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow: a stand-in mark\n")
    (tmp_path / "test_dealt.py").write_text(TESTS)
    return tmp_path


def collected(suite: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=suite,
        capture_output=True,
        text=True,
    )


def names(suite: Path, *args: str) -> list[str]:
    done = collected(suite, *args)
    assert done.returncode == 0, done.stdout + done.stderr
    return [line.split("::")[1] for line in done.stdout.splitlines() if "::" in line]


def test_the_shards_deal_the_tests_out_in_turn(suite):
    shards = [names(suite, "--shard", f"{i}/3") for i in (1, 2, 3)]
    # e, g and h are one group, dealt as one test where e first appears.
    assert shards == [
        ["test_a", "test_d", "test_i"],
        ["test_b", "test_e", "test_g", "test_h"],
        ["test_c", "test_f"],
    ]


def test_the_shards_divide_what_the_mark_expression_left(suite):
    shards = [names(suite, "-m", "not slow", "--shard", f"{i}/2") for i in (1, 2)]
    assert shards == [
        ["test_a", "test_d", "test_f"],
        ["test_b", "test_e", "test_g", "test_h", "test_i"],
    ]


def test_without_shard_every_test_is_kept(suite):
    assert len(names(suite)) == 9


@pytest.mark.parametrize("spec", ["0/2", "3/2", "2", "a/b"])
def test_a_malformed_shard_is_a_usage_error(suite, spec):
    done = collected(suite, "--shard", spec)
    assert done.returncode == pytest.ExitCode.USAGE_ERROR, done.stdout + done.stderr
    assert "--shard" in done.stderr
