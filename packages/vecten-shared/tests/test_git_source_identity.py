"""A git source's identity: the directory checked out of a fetched commit, its
`source_sha256`, its record in `extensions.lock.json`, and the image build's verify step."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vecten_shared import git_sources
from vecten_shared.extension_record import Extension, GitOrigin, parse_record, record_text
from vecten_shared.git_sources import SOURCE_REF, checkout, source_sha256
from vecten_shared.output import ExitCode, Failure

# The tree `_files` writes, hashed with `shasum` over its documented entries: every runner
# must find this value.
TREE_SHA256 = "09c9a731f01eebbefc633fe69413811a925dc0fe2519c9a88eafdadc4b46f190"
COMMIT = "3f2c" + "0" * 34 + "e1"
SHA = "a" * 64


def _git(*args: str, cwd: Path) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


FILES = {
    "pyproject.toml": b'[project]\nname = "acme"\nversion = "1.2.0"\n',
    "src/acme/__init__.py": b"skill = None\n",
    "src/acme/data/table.csv": b"a,b\n1,2\n",
    "bin/run": b"#!/bin/sh\necho run\n",
}


def _files(directory: Path, order=None) -> Path:
    for relative in order or FILES:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(FILES[relative])
        path.chmod(0o755 if relative == "bin/run" else 0o644)
    return directory


def _bare(tmp_path: Path, populate) -> tuple[Path, str]:
    """A bare repository holding one commit of the tree `populate` writes, at SOURCE_REF."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    populate(work)
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "one", cwd=work)
    commit = _git("rev-parse", "HEAD", cwd=work)
    bare = tmp_path / "repo.git"
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    _git("update-ref", SOURCE_REF, commit, cwd=bare)
    return bare, commit


def test_the_hash_of_a_known_tree_is_the_same_on_every_runner(tmp_path):
    assert source_sha256(_files(tmp_path)) == TREE_SHA256


def test_the_hash_ignores_file_order_and_modification_times(tmp_path):
    first = _files(tmp_path / "a")
    second = _files(tmp_path / "b", order=reversed(list(FILES)))
    for path in second.rglob("*"):
        os.utime(path, (1_000_000, 1_000_000))
    assert source_sha256(first) == source_sha256(second)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: (d / "src/acme/__init__.py").write_bytes(b"skill = 1\n"),
        lambda d: (d / "src/acme/data/table.csv").rename(d / "src/acme/table.csv"),
        lambda d: (d / "bin/run").chmod(0o644),
        lambda d: (d / "src/acme/__init__.py").chmod(0o755),
        lambda d: (d / "new.txt").write_bytes(b""),
    ],
    ids=["bytes", "path", "exec-bit-removed", "exec-bit-added", "new-empty-file"],
)
def test_the_hash_changes_with_any_files_bytes_path_or_executable_bit(tmp_path, change):
    before = source_sha256(_files(tmp_path))
    change(tmp_path)
    assert source_sha256(tmp_path) != before


def test_the_hash_skips_git_metadata_and_empty_directories(tmp_path):
    before = source_sha256(_files(tmp_path / "a"))
    after = _files(tmp_path / "b")
    (after / ".git").mkdir()
    (after / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (after / "empty").mkdir()
    assert source_sha256(after) == before


def test_the_hash_refuses_a_symbolic_link_naming_it(tmp_path):
    _files(tmp_path)
    (tmp_path / "src" / "acme" / "link").symlink_to("../../pyproject.toml")
    with pytest.raises(Failure) as caught:
        source_sha256(tmp_path)
    assert caught.value.code == ExitCode.REFUSED
    assert "src/acme/link" in caught.value.message
    assert "symbolic link" in caught.value.message


def test_checkout_writes_the_commits_tree_with_its_executable_bits(tmp_path):
    bare, _ = _bare(tmp_path, _files)
    into = checkout(bare, tmp_path / "copy")
    assert {p.relative_to(into).as_posix() for p in into.rglob("*") if p.is_file()} == set(FILES)
    assert (into / "bin/run").stat().st_mode & 0o100
    assert not (into / "pyproject.toml").stat().st_mode & 0o100
    assert source_sha256(into) == TREE_SHA256


def test_checkout_of_a_subdirectory_holds_only_that_directory(tmp_path):
    def populate(work):
        _files(work / "extensions" / "acme")
        (work / "README.md").write_text("monorepo\n")

    bare, _ = _bare(tmp_path, populate)
    into = checkout(bare, tmp_path / "copy", "extensions/acme")
    assert (into / "pyproject.toml").is_file()
    assert not (into / "README.md").exists()
    assert source_sha256(into) == TREE_SHA256


def test_checkout_ignores_the_repositorys_export_attributes(tmp_path):
    def populate(work):
        _files(work)
        (work / ".gitattributes").write_text("src/acme/__init__.py export-ignore\n")

    bare, _ = _bare(tmp_path, populate)
    into = checkout(bare, tmp_path / "copy")
    assert (into / "src/acme/__init__.py").read_bytes() == FILES["src/acme/__init__.py"]


def test_checkout_refuses_a_symbolic_link_naming_it(tmp_path):
    def populate(work):
        _files(work / "acme")
        (work / "acme" / "src" / "escape").symlink_to("/etc")

    bare, _ = _bare(tmp_path, populate)
    with pytest.raises(Failure) as caught:
        checkout(bare, tmp_path / "copy", "acme")
    assert caught.value.code == ExitCode.REFUSED
    assert "src/escape" in caught.value.message
    assert "symbolic link" in caught.value.message


def test_checkout_refuses_a_subdirectory_the_commit_lacks(tmp_path):
    bare, commit = _bare(tmp_path, _files)
    with pytest.raises(Failure) as caught:
        checkout(bare, tmp_path / "copy", "extensions/missing")
    assert "extensions/missing" in caught.value.message
    assert commit in caught.value.message


def test_checkout_refuses_a_destination_that_is_not_empty(tmp_path):
    bare, _ = _bare(tmp_path, _files)
    (tmp_path / "copy").mkdir()
    (tmp_path / "copy" / "left").write_text("")
    with pytest.raises(Failure, match="empty"):
        checkout(bare, tmp_path / "copy")


# --- the record -----------------------------------------------------------------------------


def _git_entry(**origin) -> Extension:
    values = {
        "url": "https://github.com/acme/extensions",
        "commit": COMMIT,
        "subdirectory": "extensions/acme",
        "tag": "acme/v1.2.0",
        "public": False,
        **origin,
    }
    return Extension(
        version="1.2.0",
        extras=("platform",),
        git=GitOrigin(**values),
        source_sha256=SHA,
        build_requires=("hatchling==1.26.3",),
    )


def test_a_git_entry_is_recorded_with_its_source_and_hash():
    text = record_text({"acme": _git_entry()})
    entry = json.loads(text)["extensions"]["acme"]
    assert entry["source"] == {
        "url": "https://github.com/acme/extensions",
        "commit": COMMIT,
        "subdirectory": "extensions/acme",
        "tag": "acme/v1.2.0",
        "public": False,
    }
    assert entry["source_sha256"] == SHA
    assert entry["build_requires"] == ["hatchling==1.26.3"]
    assert parse_record(text) == {"acme": _git_entry()}


def test_a_git_entry_without_a_tag_or_subdirectory_round_trips():
    entry = _git_entry(tag=None, subdirectory=None, public=True)
    assert parse_record(record_text({"acme": entry})) == {"acme": entry}


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.__setitem__("url", "https://x:tok@github.com/acme/extensions"),
        lambda s: s.__setitem__("url", "http://github.com/acme/extensions"),
        lambda s: s.__setitem__("url", "https://github.com/acme/extensions?ref=main"),
        lambda s: s.__setitem__("commit", "3f2c"),
        lambda s: s.__setitem__("commit", "main"),
        lambda s: s.__setitem__("subdirectory", "../up"),
        lambda s: s.__setitem__("tag", "v1..v2"),
        lambda s: s.__setitem__("public", "yes"),
        lambda s: s.__setitem__("path", "extensions/acme"),
        lambda s: s.pop("commit"),
    ],
)
def test_a_malformed_git_entry_makes_the_record_untrusted(change):
    body = json.loads(record_text({"acme": _git_entry()}))
    change(body["extensions"]["acme"]["source"])
    assert parse_record(json.dumps(body)) is None


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, None])
def test_a_git_entry_needs_a_well_formed_source_hash(digest):
    body = json.loads(record_text({"acme": _git_entry()}))
    body["extensions"]["acme"]["source_sha256"] = digest
    assert parse_record(json.dumps(body)) is None


# --- verify, as the image build runs it -----------------------------------------------------


def _verify(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vecten_shared.git_sources", "verify", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _bundle(tmp_path: Path, digest: str) -> tuple[Path, Path]:
    extensions = tmp_path / "extensions"
    _files(extensions / "acme")
    _files(extensions / "portal")
    record = {
        "acme": Extension(
            version="1.2.0",
            extras=(),
            git=GitOrigin("https://github.com/acme/extensions", COMMIT, None, "v1.2.0", False),
            source_sha256=digest,
        ),
        "portal": Extension(
            version="0.1.0", extras=(), source_path="extensions/portal", metadata_sha256=SHA
        ),
    }
    path = tmp_path / "extensions.lock.json"
    path.write_text(record_text(record))
    return extensions, path


def test_verify_accepts_every_git_sourced_directory_that_matches_its_record(tmp_path):
    extensions, record = _bundle(tmp_path, TREE_SHA256)
    result = _verify(str(extensions), str(record))
    assert result.returncode == 0, result.stderr
    assert f"acme at {COMMIT}" in result.stdout
    assert "portal" not in result.stdout


def test_verify_refuses_a_tampered_source_naming_the_extension_commit_and_both_hashes(tmp_path):
    extensions, record = _bundle(tmp_path, TREE_SHA256)
    (extensions / "acme" / "src" / "acme" / "__init__.py").write_text("import os\n")
    result = _verify(str(extensions), str(record))
    assert result.returncode == ExitCode.REFUSED
    actual = source_sha256(extensions / "acme")
    for named in ("acme", COMMIT, TREE_SHA256, actual):
        assert named in result.stderr


def test_verify_refuses_a_missing_directory_and_an_unreadable_record(tmp_path):
    extensions, record = _bundle(tmp_path, TREE_SHA256)
    shutil.rmtree(extensions / "acme")
    missing = _verify(str(extensions), str(record))
    assert missing.returncode == ExitCode.REFUSED
    assert "acme" in missing.stderr
    record.write_text("{")
    assert _verify(str(extensions), str(record)).returncode == ExitCode.REFUSED


def test_verify_takes_exactly_a_directory_and_a_record(tmp_path):
    assert _verify(str(tmp_path)).returncode == ExitCode.USAGE


# --- serving recorded commits to uv ---------------------------------------------------------


def test_one_served_repository_lists_every_commit_as_a_branch(tmp_path):
    first, one = _bare(tmp_path / "a", _files)
    second, two = _bare(tmp_path / "b", lambda work: _files(work / "other"))
    served = git_sources.serve([(first, one), (second, two)], tmp_path / "served.git")
    listed = _git("ls-remote", f"file://{served}", "HEAD", "refs/heads/*", cwd=tmp_path)
    found = {row.split()[0] for row in listed.splitlines()}
    assert {one, two} <= found
    assert _git("rev-parse", "HEAD", cwd=served) == one


def _bare_with_paths(tmp_path: Path, paths: list[str]) -> Path:
    """A bare repository whose SOURCE_REF commit holds exactly `paths`, built without a work
    tree, so names a case-insensitive file system would merge can coexist."""
    bare = tmp_path / "plumbed.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    env_index = tmp_path / "index"
    # git on macOS precomposes decomposed names in its arguments unless told not to.
    plain = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_INDEX_FILE": str(env_index),
    }
    for path in paths:
        blob = (
            subprocess.run(
                ["git", "--git-dir", str(bare), "hash-object", "-w", "--stdin"],
                input=path.encode(),
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        subprocess.run(
            ["git", "-c", "core.precomposeunicode=false", "--git-dir", str(bare)]
            + ["update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"],
            env=plain,
            check=True,
            capture_output=True,
        )
    tree = subprocess.run(
        ["git", "--git-dir", str(bare), "write-tree"],
        env=plain,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    commit = _git("--git-dir", str(bare), "commit-tree", tree, "-m", "plumbed", cwd=tmp_path)
    _git("--git-dir", str(bare), "update-ref", SOURCE_REF, commit, cwd=tmp_path)
    return bare


@pytest.mark.parametrize(
    "paths",
    [
        ["Foo/a.py", "foo/b.py"],
        ["A", "a/b.py"],
        ["README", "readme"],
        ["café.txt", "café.txt"],
        ["src/Café/x.py", "src/café/y.py"],
    ],
    ids=["directories-by-case", "file-beside-directory", "files-by-case", "nfc-nfd", "nested"],
)
def test_checkout_refuses_paths_a_file_system_could_merge(tmp_path, paths):
    bare = _bare_with_paths(tmp_path, paths)
    into = tmp_path / "copy"
    with pytest.raises(Failure) as caught:
        checkout(bare, into)
    assert caught.value.code == ExitCode.REFUSED
    assert "collide" in caught.value.message
    assert not into.exists() or not any(into.iterdir())


def test_checkout_keeps_paths_that_differ_in_more_than_case(tmp_path):
    bare = _bare_with_paths(tmp_path, ["Foo/a.py", "food/b.py", "A.py"])
    into = checkout(bare, tmp_path / "copy")
    assert sorted(p.relative_to(into).as_posix() for p in into.rglob("*.py")) == [
        "A.py",
        "Foo/a.py",
        "food/b.py",
    ]


@pytest.mark.parametrize("where", [".git", "src/acme/.git", "src/.GIT"])
def test_verify_refuses_git_metadata_anywhere_in_a_git_sourced_directory(tmp_path, where):
    extensions, record = _bundle(tmp_path, TREE_SHA256)
    (extensions / "acme" / where).mkdir(parents=True)
    (extensions / "acme" / where / "HEAD").write_text("ref: refs/heads/main\n")
    result = _verify(str(extensions), str(record))
    assert result.returncode == ExitCode.REFUSED
    assert where in result.stderr
