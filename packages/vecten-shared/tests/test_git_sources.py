"""Git sources of extensions: parsing, credentials, the isolated git environment, ref
resolution, fetch and the failures they raise, against a local `git http-backend`."""

from __future__ import annotations

import base64
import importlib.util
import io
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from vecten_shared import git_sources
from vecten_shared.git_sources import (
    Cause,
    Credentials,
    GitSourceFailure,
    fetch,
    git_environment,
    parse_source,
    resolve,
)
from vecten_shared.output import ExitCode, Failure

COMMIT = "3f2c" + "0" * 34 + "e1"


def _load_git_http():
    # Every package's test directory is a package named `tests`, so a relative import
    # resolves against whichever one pytest imported first; load the helper by path.
    name = "vecten_shared_tests_git_http"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("git_http.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _token() -> str:
    # Letters that are no hex digit and no letter of the words a failure prints, so a
    # four-character window of the token cannot appear in a message by chance.
    return "".join(secrets.choice("GHJKMNPQRSTUVWXYZ") for _ in range(32))


def _leaks(token: str, text: str) -> list[str]:
    return [token[i : i + 4] for i in range(len(token) - 3) if token[i : i + 4] in text]


# --- 1.1 parsing --------------------------------------------------------------------------


def test_parse_a_prefixed_tag_with_extras_and_subdirectory():
    source = parse_source(
        "acme-resolution[cli,mcp] @ git+https://GitHub.com/acme/extensions"
        "@acme-resolution/v1.2.0#subdirectory=extensions/acme-resolution"
    )
    assert source.name == "acme-resolution"
    assert source.extras == ("cli", "mcp")
    assert source.host == "github.com"
    assert source.repository == "acme/extensions"
    assert source.ref == "acme-resolution/v1.2.0"
    assert source.subdirectory == "extensions/acme-resolution"
    assert source.url == "https://github.com/acme/extensions"


def test_parse_a_full_commit_with_a_port_and_no_subdirectory():
    source = parse_source(f"acme @ git+https://git.example.com:8443/team/acme.git@{COMMIT}")
    assert (source.host, source.repository, source.ref) == (
        "git.example.com:8443",
        "team/acme.git",
        COMMIT,
    )
    assert source.subdirectory is None
    assert source.extras == ()


def test_requirement_line_holds_the_commit_and_names_the_tag():
    source = parse_source(
        "acme[mcp] @ git+https://github.com/acme/extensions@acme/v1.0.0#subdirectory=acme"
    )
    assert source.requirement_line(COMMIT, "acme/v1.0.0") == (
        f"acme[mcp] @ git+https://github.com/acme/extensions@{COMMIT}#subdirectory=acme"
        "  # tag acme/v1.0.0"
    )
    assert source.requirement_line(COMMIT, None) == (
        f"acme[mcp] @ git+https://github.com/acme/extensions@{COMMIT}#subdirectory=acme"
    )
    again = parse_source(source.requirement_line(COMMIT, None))
    assert (again.repository, again.ref, again.subdirectory) == ("acme/extensions", COMMIT, "acme")


@pytest.mark.parametrize(
    ("url", "says"),
    [
        ("git+http://github.com/acme/extensions@v1", "git+https"),
        ("git+ssh://git@github.com/acme/extensions@v1", "git+https"),
        ("git+file:///srv/acme/extensions@v1", "git+https"),
        ("https://github.com/acme/extensions@v1", "git+https"),
        ("file:///srv/acme/extensions", "git+https"),
        ("git+https://github.com/acme/extensions", "tag or a full commit"),
        ("git+https://github.com/acme/extensions@", "tag or a full commit"),
        ("git+https://github.com/acme/extensions@v1?ref=main", "query"),
        ("git+https://github.com/acme/extensions@v1#egg=acme", "subdirectory"),
        ("git+https://github.com/acme/extensions@v1#subdirectory=/abs", "subdirectory"),
        ("git+https://github.com/acme/extensions@v1#subdirectory=../up", "subdirectory"),
        ("git+https://github.com/acme/extensions@v1#subdirectory=a/../../up", "subdirectory"),
        ("git+https://github.com/acme/extensions@v1#subdirectory=", "subdirectory"),
        ("git+https://github.com/acme/extensions@-v1", "ref"),
        ("git+https://github.com/acme/extensions@v1..v2", "ref"),
        ("git+https://github.com/acme/extensions@v1~2", "ref"),
        ("git+https://github.com/acme/../extensions@v1", "repository"),
        ("git+https:///acme/extensions@v1", "host"),
    ],
)
def test_refused_sources_never_echo_the_url(url, says):
    with pytest.raises(Failure) as caught:
        parse_source(f"acme @ {url}")
    assert caught.value.code == ExitCode.USAGE
    assert says in caught.value.message
    assert "acme/extensions" not in caught.value.message
    assert "/srv/" not in caught.value.message


def test_credentials_in_the_url_are_refused_without_echoing_them():
    with pytest.raises(Failure) as caught:
        parse_source("acme @ git+https://x:secret@github.com/acme/extensions@v1")
    assert "GIT_CREDENTIALS" in caught.value.message
    assert "secret" not in caught.value.message
    assert "github.com/acme" not in caught.value.message


@pytest.mark.parametrize(
    "text",
    [
        'acme @ git+https://github.com/acme/extensions@v1 ; python_version > "3.10"',
        "acme @ git+https://github.com/acme/extensions@v1\n--index-url https://evil",
        "not a requirement @@ git+https://github.com/acme/extensions@v1",
        "acme",
    ],
)
def test_markers_newlines_and_other_forms_are_refused(text):
    with pytest.raises(Failure) as caught:
        parse_source(text)
    assert caught.value.code == ExitCode.USAGE
    assert "acme/extensions" not in caught.value.message


# --- 1.2 credentials ----------------------------------------------------------------------


def test_credentials_parse_whitespace_separated_entries():
    token, other = _token(), _token()
    credentials = Credentials.parse(
        f" https://x-access-token:{token}@GitHub.com/acme\n"
        f"\thttps://oauth2:{other}@gitlab.example.com "
    )
    assert [(c.host, c.path) for c in credentials.entries] == [
        ("github.com", "acme"),
        ("gitlab.example.com", ""),
    ]
    assert token not in repr(credentials)
    assert Credentials.parse(None).entries == ()
    assert Credentials.parse("  ").entries == ()


@pytest.mark.parametrize(
    "entry",
    [
        "https://{t}@github.com",
        "https://user:@github.com",
        "http://user:{t}@github.com",
        "https://user:{t}@github.com/acme?x=1",
        "https://user:{t}@github.com#frag",
        "user:{t}@github.com",
        "https://user:{t}@",
    ],
)
def test_malformed_entries_are_refused_by_position_never_by_value(entry):
    token = _token()
    with pytest.raises(Failure) as caught:
        Credentials.parse(f"https://ok:{_token()}@example.com {entry.format(t=token)}")
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "GIT_CREDENTIALS" in caught.value.message
    assert "entry 2" in caught.value.message
    assert _leaks(token, caught.value.message) == []
    assert "github.com" not in caught.value.message


def test_the_longest_path_on_the_host_wins():
    credentials = Credentials.parse(
        f"https://h:{_token()}@github.com https://o:{_token()}@github.com/acme "
        f"https://r:{_token()}@github.com/acme/extensions https://p:{_token()}@github.com:8443"
    )

    def chosen(host, repository):
        entry = credentials.for_repository(host, repository)
        return entry and entry.username

    assert chosen("github.com", "acme/extensions.git") == "r"
    assert chosen("github.com", "acme/other") == "o"
    assert chosen("github.com", "acme-other/tools") == "h"
    assert chosen("GitHub.com", "beta/tools") == "h"
    assert chosen("github.com:8443", "acme/x") == "p"
    assert chosen("gitlab.com", "acme/x") is None


def test_the_credential_file_is_private_names_one_repository_and_is_deleted():
    token = _token()
    credentials = Credentials.parse(f"https://o:{token}@github.com/acme https://h:t0k@gitlab.com")
    source = parse_source("acme @ git+https://github.com/acme/extensions.git@v1")
    with git_sources.credential_environment(source, credentials) as run:
        env = run.env
        values = [env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
        keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
        assert keys[0] == "credential.helper" and values[0] == ""
        assert dict(zip(keys[1:], values[1:], strict=True))["credential.useHttpPath"] == "true"
        assert all(token not in value for value in env.values())
        stored = run.credential_file
        assert stat.S_IMODE(stored.stat().st_mode) == 0o600
        assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
        assert stored.read_text() == f"https://o:{token}@github.com/acme/extensions.git\n"
    assert not stored.exists()
    assert not stored.parent.exists()


def test_a_host_entry_is_written_for_the_host_alone():
    credentials = Credentials.parse("https://h:t0k@github.com")
    source = parse_source("acme @ git+https://github.com/acme/extensions@v1")
    with git_sources.credential_environment(source, credentials) as run:
        keys = [run.env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(run.env["GIT_CONFIG_COUNT"]))]
        assert "credential.useHttpPath" not in keys
        assert run.credential_file.read_text() == "https://h:t0k@github.com\n"


def test_no_matching_entry_gives_no_helper():
    source = parse_source("acme @ git+https://github.com/acme/extensions@v1")
    with git_sources.credential_environment(source, Credentials.parse("")) as run:
        assert run.env["GIT_CONFIG_COUNT"] == "1"
        assert run.env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert run.credential_file is None


# --- 1.3 the isolated environment ---------------------------------------------------------


def test_git_environment_ignores_user_configuration_prompts_and_traces():
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/me",
        "HTTPS_PROXY": "http://proxy:3128",
        "GIT_SSL_CAINFO": "/etc/proxy-ca.pem",
        "GIT_TRACE": "1",
        "GIT_TRACE_CURL": "/tmp/curl.log",
        "GIT_TRACE_PACKET": "1",
        "GIT_CURL_VERBOSE": "1",
        "GIT_ASKPASS": "/usr/bin/askpass",
        "SSH_ASKPASS": "/usr/bin/ssh-askpass",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "osxkeychain",
        "GIT_CONFIG_PARAMETERS": "'credential.helper'='store'",
        "GIT_DIR": "/somewhere/.git",
        "GIT_SSL_NO_VERIFY": "1",
    }
    env = git_environment(parent)
    assert env["PATH"] == "/usr/bin"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["GIT_SSL_CAINFO"] == "/etc/proxy-ca.pem"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert env["LC_ALL"] == "C"
    for name in (
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_TRACE_PACKET",
        "GIT_CURL_VERBOSE",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_SSL_NO_VERIFY",
    ):
        assert name not in env
    assert "GIT_CONFIG_COUNT" not in env


# --- 1.6 masking --------------------------------------------------------------------------


def test_mask_removes_tokens_entries_url_user_information_and_authorization():
    token = _token() + "%2F" + _token()
    credentials = Credentials.parse(f"https://x-access-token:{token}@github.com/acme")
    decoded = token.replace("%2F", "/")
    text = (
        f"fatal: https://x-access-token:{token}@github.com/acme failed\n"
        f"token {decoded} and https://someone:else@host/path\n"
        "> Authorization: Basic eDp5\n"
        "Proxy-Authorization: Bearer abc.def\n"
    )
    masked = credentials.mask(text)
    for secret in (token, decoded, "someone:else", "eDp5", "abc.def"):
        assert secret not in masked
    assert "https://***@host/path" in masked
    assert "Authorization: ***" in masked


def test_tokens_are_registered_with_github_masking_in_actions(monkeypatch):
    token, other = _token(), _token()
    credentials = Credentials.parse(f"https://x:{token}@github.com https://y:{other}@gitlab.com")
    out = io.StringIO()
    credentials.announce({"GITHUB_ACTIONS": "true"}, out)
    assert out.getvalue().splitlines() == [f"::add-mask::{token}", f"::add-mask::{other}"]
    again = io.StringIO()
    credentials.announce({"GITHUB_ACTIONS": "true"}, again)
    assert again.getvalue() == ""
    quiet = io.StringIO()
    Credentials.parse(f"https://z:{_token()}@github.com").announce({}, quiet)
    Credentials.parse(f"https://z:{_token()}@github.com").announce(
        {"GITHUB_ACTIONS": "false"}, quiet
    )
    assert quiet.getvalue() == ""


def test_announcements_go_to_stderr_so_stdout_stays_one_json_document(capsys):
    token = _token()
    Credentials.parse(f"https://x:{token}@github.com").announce({"GITHUB_ACTIONS": "true"})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"::add-mask::{token}\n"


def test_the_credential_file_lives_in_shared_memory_when_the_host_has_it(tmp_path, monkeypatch):
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(git_sources, "SHARED_MEMORY", shm)
    source = parse_source("acme @ git+https://github.com/acme/extensions@v1")
    with git_sources.credential_environment(
        source, Credentials.parse("https://h:t0k@github.com")
    ) as run:
        assert run.credential_file.is_relative_to(shm)
    assert list(shm.iterdir()) == []
    monkeypatch.setattr(git_sources, "SHARED_MEMORY", tmp_path / "absent")
    with git_sources.credential_environment(
        source, Credentials.parse("https://h:t0k@github.com")
    ) as run:
        assert run.credential_file.is_relative_to(Path(tempfile.gettempdir()).resolve()) or (
            run.credential_file.is_relative_to(Path(tempfile.gettempdir()))
        )


SIGTERM_CHILD = """
import sys, time
from vecten_shared import git_sources
source = git_sources.parse_source("acme @ git+https://github.com/acme/extensions@v1")
credentials = git_sources.Credentials.parse("https://h:t0k@github.com")
with git_sources.credential_environment(source, credentials) as run:
    print(run.credential_file, flush=True)
    time.sleep(60)
"""


def test_the_credential_file_is_deleted_when_the_process_is_terminated(tmp_path):
    with subprocess.Popen(
        [sys.executable, "-c", SIGTERM_CHILD], stdout=subprocess.PIPE, text=True
    ) as child:
        try:
            stored = Path(child.stdout.readline().strip())
            assert stored.is_file()
            child.terminate()
            assert child.wait(timeout=30) != 0
        finally:
            child.kill()
    assert not stored.exists()
    assert not stored.parent.exists()


def test_git_output_that_is_not_utf8_is_decoded_with_replacement(tmp_path):
    result = git_sources._git(
        ["check-ref-format", "--print", "refs/heads/a\udcffb"], git_sources._local(tmp_path)
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "refs/heads/a\ufffdb"


@pytest.mark.parametrize(("commit", "tag"), [(COMMIT[:12], None), (COMMIT, "-v1"), (COMMIT, "a:b")])
def test_fetch_refuses_a_malformed_recorded_commit_or_tag(tmp_path, commit, tag):
    source = parse_source(f"acme @ git+https://git.invalid/acme/extensions@{COMMIT}")
    with pytest.raises(Failure) as caught:
        fetch(source, commit, tmp_path / "r", None, tag=tag)
    assert caught.value.code == ExitCode.REFUSED
    assert "vecten extension add" in caught.value.message
    assert not (tmp_path / "r").exists()


def test_fetch_refuses_a_destination_that_is_not_empty(tmp_path):
    source = parse_source(f"acme @ git+https://git.invalid/acme/extensions@{COMMIT}")
    into = tmp_path / "r"
    into.mkdir()
    (into / "config").write_text(
        '[url "https://127.0.0.1:9/"]\n\tinsteadOf = https://git.invalid/\n'
    )
    with pytest.raises(Failure) as caught:
        fetch(source, COMMIT, into, None)
    assert caught.value.code == ExitCode.REFUSED
    assert "empty" in caught.value.message


# --- 1.4, 1.5, 1.7 against a local git http-backend ---------------------------------------


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


class Remote:
    """The commits the served repositories hold, by role."""

    def __init__(self, root: Path, work: Path):
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        (work / "pyproject.toml").write_text('[project]\nname = "acme"\nversion = "1.2.0"\n')
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "one", cwd=work)
        self.tagged = _git("rev-parse", "HEAD", cwd=work)
        _git("tag", "-a", "acme/v1.2.0", "-m", "release", cwd=work)
        _git("tag", "light", cwd=work)
        _git("tag", "both", cwd=work)
        _git("branch", "both", cwd=work)
        (work / "two").write_text("2")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "two", cwd=work)
        self.middle = _git("rev-parse", "HEAD", cwd=work)
        (work / "three").write_text("3")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "three", cwd=work)
        self.tip = _git("rev-parse", "HEAD", cwd=work)
        _git("branch", "feature", cwd=work)
        for name in ("acme/extensions.git", "public/tools.git", "legacy/old.git"):
            bare = root / name
            bare.parent.mkdir(parents=True, exist_ok=True)
            _git("clone", "-q", "--mirror", str(work), str(bare), cwd=root)
        # `middle` was recorded through the tag `moved`, which has since moved to `tip`; no ref
        # advertises `middle` in any repository.
        sha = base_sha = work.parent / "sha256"
        sha.mkdir()
        _git("init", "-q", "--object-format=sha256", "-b", "main", cwd=sha)
        (sha / "pyproject.toml").write_text('[project]\nname = "acme"\nversion = "1.0.0"\n')
        _git("add", "-A", cwd=sha)
        _git("commit", "-qm", "one", cwd=base_sha)
        self.sha256 = _git("rev-parse", "HEAD", cwd=sha)
        _git("tag", "-a", "v1.0.0", "-m", "release", cwd=sha)
        (sha / "two").write_text("2")
        _git("add", "-A", cwd=sha)
        _git("commit", "-qm", "two", cwd=sha)
        _git("clone", "-q", "--mirror", str(sha), str(root / "acme/sha256.git"), cwd=root)
        legacy = root / "legacy/old.git"
        _git("tag", "moved", self.tip, cwd=legacy)
        # Only the annotated tag points at `tagged` there, so no ref advertises it but the
        # tag's peeled value.
        _git("tag", "-d", "light", "both", cwd=legacy)
        _git("branch", "-D", "both", cwd=legacy)


@pytest.fixture(scope="module")
def remote(tmp_path_factory):
    base = tmp_path_factory.mktemp("git-remote")
    root = base / "root"
    root.mkdir()
    commits = Remote(root, base / "work")
    token = _token()
    git_http = _load_git_http()
    server = git_http.GitServer(
        root=root,
        users={"x-access-token": token},
        public={"public/tools.git"},
        version_0={"legacy/old.git"},
        errors={"acme/broken.git": f"internal error for token {token}"},
    ).start(base / "tls")
    yield commits, server, token
    server.stop()


@pytest.fixture
def served(remote, monkeypatch, tmp_path):
    commits, server, token = remote
    monkeypatch.setenv("GIT_SSL_CAINFO", str(server.ca_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    server.authorizations.clear()
    host = server.url.removeprefix("https://")
    good = Credentials.parse(f"https://x-access-token:{token}@{host}/acme")

    def source(repository: str, ref: str) -> git_sources.GitSource:
        return parse_source(f"acme @ git+{server.url}/{repository}@{ref}")

    return commits, server, token, good, source, host


def test_resolve_a_prefixed_annotated_tag_to_its_commit(served):
    commits, _, _, good, source, _ = served
    resolution = resolve(source("acme/extensions.git", "acme/v1.2.0"), good)
    assert (resolution.commit, resolution.tag, resolution.public) == (
        commits.tagged,
        "acme/v1.2.0",
        False,
    )


def test_resolve_a_lightweight_tag(served):
    commits, _, _, good, source, _ = served
    assert resolve(source("acme/extensions.git", "light"), good).commit == commits.tagged


def test_resolve_a_full_commit_checks_access_without_a_tag(served):
    commits, _, _, good, source, _ = served
    resolution = resolve(source("acme/extensions.git", commits.middle), good)
    assert (resolution.commit, resolution.tag, resolution.public) == (commits.middle, None, False)


@pytest.mark.parametrize(
    ("ref", "cause", "says"),
    [
        ("feature", Cause.BRANCH, "does not pin a version"),
        ("both", Cause.BRANCH, "both a tag and a branch"),
        ("v9.9.9", Cause.REF, "no tag"),
        ("deadbeef", Cause.REF, "full commit"),
    ],
)
def test_branches_ambiguous_missing_and_abbreviated_refs_are_refused(served, ref, cause, says):
    _, _, _, good, source, host = served
    with pytest.raises(GitSourceFailure) as caught:
        resolve(source("acme/extensions.git", ref), good)
    failure = caught.value
    assert failure.cause == cause
    assert says in failure.message
    assert f"{host}/acme/extensions.git" in failure.message
    assert ref in failure.message
    assert failure.details["cause"] == cause.value


def test_abbreviated_commit_of_a_real_commit_is_refused(served):
    commits, _, _, good, source, _ = served
    with pytest.raises(GitSourceFailure) as caught:
        resolve(source("acme/extensions.git", commits.middle[:12]), good)
    assert caught.value.cause == Cause.REF


def test_a_wrong_token_is_refused_naming_the_variable_and_leaking_nothing(served, capfd):
    _, _, token, _, source, host = served
    wrong = _token()
    with pytest.raises(GitSourceFailure) as caught:
        resolve(
            source("acme/extensions.git", "acme/v1.2.0"),
            Credentials.parse(f"https://x-access-token:{wrong}@{host}"),
        )
    failure = caught.value
    assert failure.cause == Cause.AUTHENTICATION
    assert failure.code == ExitCode.PREREQUISITE
    assert "authentication refused" in failure.message
    assert "GIT_CREDENTIALS" in failure.message
    assert f"{host}/acme/extensions.git" in failure.message
    captured = capfd.readouterr()
    for shown in (failure.message, str(failure.details), captured.out, captured.err):
        assert _leaks(wrong, shown) == []
        assert _leaks(token, shown) == []


def test_a_private_repository_without_a_credential_names_the_variable(served):
    _, server, _, _, source, host = served
    with pytest.raises(GitSourceFailure) as caught:
        resolve(source("acme/extensions.git", "acme/v1.2.0"), Credentials.parse(None))
    assert caught.value.cause == Cause.AUTHENTICATION
    assert f"GIT_CREDENTIALS with an entry for https://{host}" in caught.value.message
    assert set(server.authorizations) == {None}


def test_a_missing_repository_behind_an_accepted_token(served):
    _, _, _, good, source, _ = served
    with pytest.raises(GitSourceFailure) as caught:
        resolve(source("acme/nothing.git", "v1"), good)
    assert caught.value.cause == Cause.REPOSITORY
    assert "repository not found" in caught.value.message


def test_an_unreachable_host_is_a_network_failure_with_gits_words_masked(served, tmp_path):
    _, _, token, _, _, _ = served
    source = parse_source(f"acme @ git+https://127.0.0.1:9/acme/extensions.git@{COMMIT}")
    with pytest.raises(GitSourceFailure) as caught:
        fetch(
            source,
            COMMIT,
            tmp_path / "r",
            Credentials.parse(f"https://x-access-token:{token}@127.0.0.1:9"),
        )
    failure = caught.value
    assert failure.cause == Cause.NETWORK
    assert failure.code == ExitCode.TOOL_FAILED
    assert "127.0.0.1:9/acme/extensions.git" in failure.message
    assert "  git: " in failure.message
    assert _leaks(token, failure.message) == []


def test_a_public_repository_needs_no_credential_and_is_asked_anonymously_first(served):
    commits, server, _, _, source, host = served
    anonymous = resolve(source("public/tools.git", "acme/v1.2.0"), Credentials.parse(None))
    assert (anonymous.commit, anonymous.public) == (commits.tagged, True)
    with_host_entry = resolve(
        source("public/tools.git", "light"),
        Credentials.parse(f"https://x-access-token:{_token()}@{host}"),
    )
    assert with_host_entry.public is True
    assert set(server.authorizations) == {None}


def test_fetch_a_commit_that_no_ref_points_at(served, tmp_path):
    commits, _, _, good, source, _ = served
    bare = fetch(
        source("acme/extensions.git", commits.middle), commits.middle, tmp_path / "r", good
    )
    assert _git("rev-parse", "refs/vecten/source", cwd=bare) == commits.middle
    assert _git("rev-list", "--count", "refs/vecten/source", cwd=bare) == "1"
    assert _git("rev-parse", "--is-bare-repository", cwd=bare) == "true"


def test_the_fetched_repository_serves_the_commit_as_its_head(served, tmp_path):
    # uv reads the repository through a file:// rewrite and fetches its HEAD as well.
    commits, _, _, good, source, _ = served
    bare = fetch(
        source("acme/extensions.git", commits.middle), commits.middle, tmp_path / "r", good
    )
    _git("clone", "-q", f"file://{bare}", str(tmp_path / "clone"), cwd=tmp_path)
    assert _git("rev-parse", "HEAD", cwd=tmp_path / "clone") == commits.middle


def test_fetch_a_public_commit_without_a_credential(served, tmp_path):
    commits, server, _, _, source, _ = served
    s = source("public/tools.git", commits.tagged)
    bare = fetch(s, commits.tagged, tmp_path / "r", Credentials.parse(None))
    assert _git("rev-parse", "refs/vecten/source", cwd=bare) == commits.tagged
    assert set(server.authorizations) == {None}


def test_fetch_of_a_commit_the_host_does_not_serve(served, tmp_path):
    _, _, _, good, source, host = served
    missing = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(GitSourceFailure) as caught:
        fetch(source("acme/extensions.git", missing), missing, tmp_path / "r", good)
    failure = caught.value
    assert failure.cause == Cause.COMMIT
    assert missing in failure.message
    assert f"{host}/acme/extensions.git" in failure.message
    assert "vecten extension add" in failure.message


def test_fetch_falls_back_to_the_tag_where_the_host_refuses_a_hash(served, tmp_path):
    commits, _, _, good, source, _ = served
    legacy = Credentials.parse(f"{good.entries[0].entry.rsplit('/', 1)[0]}/legacy")
    s = source("legacy/old.git", commits.tagged)
    with pytest.raises(GitSourceFailure) as caught:
        fetch(s, commits.tagged, tmp_path / "plain", legacy)
    assert caught.value.cause == Cause.COMMIT
    bare = fetch(s, commits.tagged, tmp_path / "tagged", legacy, tag="acme/v1.2.0")
    assert _git("rev-parse", "refs/vecten/source", cwd=bare) == commits.tagged


def test_fetch_through_a_moved_tag_is_not_reachable(served, tmp_path):
    commits, _, _, good, source, _ = served
    legacy = Credentials.parse(f"{good.entries[0].entry.rsplit('/', 1)[0]}/legacy")
    with pytest.raises(GitSourceFailure) as caught:
        fetch(
            source("legacy/old.git", commits.middle),
            commits.middle,
            tmp_path / "r",
            legacy,
            tag="moved",
        )
    assert caught.value.cause == Cause.COMMIT
    assert "moved" in caught.value.message


def test_a_user_credential_helper_in_the_global_config_is_left_unused(
    served, tmp_path, monkeypatch
):
    commits, server, _, good, source, _ = served
    marker = tmp_path / "helper-ran"
    config = tmp_path / "home" / ".gitconfig"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "[credential]\n"
        f'\thelper = "!f() {{ touch {marker}; echo username=x; echo password=nope; }}; f"\n'
        # A rewrite to a closed port: honouring the user's configuration fails every command.
        f'[url "https://127.0.0.1:9/"]\n\tinsteadOf = {server.url}/\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"!f() {{ touch {marker}; }}; f")
    with pytest.raises(GitSourceFailure):
        resolve(source("acme/extensions.git", "acme/v1.2.0"), Credentials.parse(None))
    assert resolve(source("acme/extensions.git", "acme/v1.2.0"), good).commit == commits.tagged
    fetch(source("acme/extensions.git", commits.tip), commits.tip, tmp_path / "r", good)
    assert not marker.exists()


def test_a_netrc_in_the_users_home_is_left_unused(served, tmp_path, monkeypatch):
    _, server, token, _, source, _ = served
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    netrc = f"machine 127.0.0.1 login x-access-token password {token}\n"
    (home / ".netrc").write_text(netrc)
    (home / ".netrc").chmod(0o600)
    monkeypatch.setenv("NETRC", str(home / ".netrc"))
    with pytest.raises(GitSourceFailure) as caught:
        resolve(source("acme/extensions.git", "acme/v1.2.0"), Credentials.parse(None))
    assert caught.value.cause == Cause.AUTHENTICATION
    assert set(server.authorizations) == {None}


def test_trace_variables_in_the_parent_environment_leak_nothing(
    served, tmp_path, monkeypatch, capfd
):
    commits, _, token, good, source, _ = served
    trace = tmp_path / "trace"
    for name in ("GIT_TRACE", "GIT_TRACE_CURL", "GIT_TRACE_PACKET", "GIT_TRACE_SETUP"):
        monkeypatch.setenv(name, str(trace))
    monkeypatch.setenv("GIT_CURL_VERBOSE", "1")
    monkeypatch.setenv("GIT_TRACE_REDACT", "0")
    resolve(source("acme/extensions.git", "acme/v1.2.0"), good)
    fetch(source("acme/extensions.git", commits.tip), commits.tip, tmp_path / "r", good)
    captured = capfd.readouterr()
    assert not trace.exists()
    assert _leaks(token, captured.out + captured.err) == []


def test_the_credential_file_is_gone_after_resolve_and_fetch(served, tmp_path, monkeypatch):
    commits, _, _, good, source, _ = served
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setattr(git_sources, "SHARED_MEMORY", tmp_path / "absent")
    resolve(source("acme/extensions.git", "acme/v1.2.0"), good)
    fetch(source("acme/extensions.git", commits.tip), commits.tip, tmp_path / "r", good)
    with pytest.raises(GitSourceFailure):
        resolve(source("acme/extensions.git", "feature"), good)
    assert list(scratch.iterdir()) == []


def test_tokens_are_announced_on_stderr_before_git_runs_in_actions(
    served, tmp_path, monkeypatch, capfd
):
    commits, server, _, _, source, host = served
    token = _token()
    server.users["announced"] = token
    credentials = Credentials.parse(f"https://announced:{token}@{host}/acme")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    real = git_sources._git

    def marked(args, run):
        sys.stderr.write("GIT RUNS\n")
        sys.stderr.flush()
        return real(args, run)

    monkeypatch.setattr(git_sources, "_git", marked)
    fetch(source("acme/extensions.git", commits.tip), commits.tip, tmp_path / "r", credentials)
    fetch(source("acme/extensions.git", commits.tip), commits.tip, tmp_path / "s", credentials)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err.index(f"::add-mask::{token}") < captured.err.index("GIT RUNS")
    assert captured.err.count(token) == captured.err.count("::add-mask::") == 1


def test_a_sha256_repository_by_tag_and_by_commit(served, tmp_path):
    commits, _, _, good, source, _ = served
    resolution = resolve(source("acme/sha256.git", "v1.0.0"), good)
    assert resolution.commit == commits.sha256
    assert len(resolution.commit) == 64
    bare = fetch(source("acme/sha256.git", commits.sha256), commits.sha256, tmp_path / "r", good)
    assert _git("rev-parse", "refs/vecten/source", cwd=bare) == commits.sha256


def test_the_callers_repository_configuration_is_left_unused(served, tmp_path, monkeypatch):
    commits, server, token, good, source, _ = served
    marker = tmp_path / "helper-ran"
    project = tmp_path / "project"
    project.mkdir()
    _git("init", "-q", cwd=project)
    header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    with (project / ".git" / "config").open("a") as config:
        config.write(
            # A rewrite to a closed port, as a caller's checkout might carry: honouring it
            # fails every command.
            f'[url "https://127.0.0.1:9/"]\n\tinsteadOf = {server.url}/\n'
            # actions/checkout leaves a header like this in the checkout's configuration.
            f'[http "{server.url}/"]\n\textraheader = Authorization: Basic {header}\n'
            f'[credential]\n\thelper = "!f() {{ touch {marker}; }}; f"\n'
        )
    (project / "sub").mkdir()
    monkeypatch.chdir(project / "sub")
    public = resolve(source("public/tools.git", "acme/v1.2.0"), Credentials.parse(None))
    assert (public.commit, public.public) == (commits.tagged, True)
    assert set(server.authorizations) == {None}
    assert resolve(source("acme/extensions.git", "acme/v1.2.0"), good).public is False
    fetch(source("acme/extensions.git", commits.tip), commits.tip, tmp_path / "r", good)
    assert not marker.exists()


def test_git_output_holding_a_token_is_shown_masked(served, tmp_path):
    _, _, token, good, source, _ = served
    with pytest.raises(GitSourceFailure) as caught:
        fetch(source("acme/broken.git", COMMIT), COMMIT, tmp_path / "r", good)
    failure = caught.value
    assert "remote error: internal error for token ***" in failure.message
    assert _leaks(token, failure.message) == []


def test_the_empty_helper_clears_one_configured_where_vecten_cannot_switch_it_off(
    served, tmp_path, monkeypatch
):
    # A helper in configuration git reads regardless, such as the one Apple's git builds in.
    commits, _, _, good, source, _ = served
    marker = tmp_path / "helper-ran"
    config = tmp_path / "builtin.gitconfig"
    config.write_text(f'[credential]\n\thelper = "!f() {{ touch {marker}; }}; f"\n')
    isolated = git_sources.git_environment

    def with_builtin(base=None):
        return {**isolated(base), "GIT_CONFIG_GLOBAL": str(config)}

    monkeypatch.setattr(git_sources, "git_environment", with_builtin)
    with pytest.raises(GitSourceFailure):
        resolve(source("acme/extensions.git", "acme/v1.2.0"), Credentials.parse(None))
    resolve(source("acme/extensions.git", "acme/v1.2.0"), good)
    assert not marker.exists()


def test_a_commit_gone_from_a_host_that_fetches_by_hash_is_not_reachable(served, tmp_path):
    _, _, _, good, source, _ = served
    missing = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(GitSourceFailure) as caught:
        fetch(source("acme/extensions.git", missing), missing, tmp_path / "r", good, tag="light")
    assert caught.value.cause == Cause.COMMIT
    assert "refuses to fetch by commit" not in caught.value.message
    assert "light" in caught.value.message


def test_a_failed_update_of_the_fetched_tag_is_reported(served, tmp_path, monkeypatch):
    commits, _, _, good, source, _ = served
    legacy = Credentials.parse(f"{good.entries[0].entry.rsplit('/', 1)[0]}/legacy")
    real = git_sources._git

    def failing_update(args, run):
        if args[0] == "update-ref":
            return subprocess.CompletedProcess(args, 1, "", "fatal: cannot lock ref")
        return real(args, run)

    monkeypatch.setattr(git_sources, "_git", failing_update)
    with pytest.raises(Failure) as caught:
        fetch(
            source("legacy/old.git", commits.tagged),
            commits.tagged,
            tmp_path / "r",
            legacy,
            tag="acme/v1.2.0",
        )
    assert caught.value.code == ExitCode.TOOL_FAILED


# --- 1.8 a public GitHub repository -------------------------------------------------------

PACKAGING = "https://github.com/pypa/packaging"
# The commit the release tag 26.0 of pypa/packaging peels to.
PACKAGING_26_0 = "3b77a26f5a27473ad3b08194d773f325d018a2d0"


@pytest.mark.network
def test_a_public_github_repository_by_tag_by_commit_and_with_a_subdirectory(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    nobody = Credentials.parse(None)
    by_tag = parse_source(f"packaging @ git+{PACKAGING}@26.0#subdirectory=src/packaging")
    resolution = resolve(by_tag, nobody)
    assert (resolution.commit, resolution.tag, resolution.public) == (PACKAGING_26_0, "26.0", True)

    by_commit = parse_source(
        f"packaging @ git+{PACKAGING}@{PACKAGING_26_0}#subdirectory=src/packaging"
    )
    assert resolve(by_commit, nobody).commit == PACKAGING_26_0
    bare = fetch(by_commit, PACKAGING_26_0, tmp_path / "r", nobody)
    assert _git("rev-list", "--count", "refs/vecten/source", cwd=bare) == "1"
    assert (
        _git("cat-file", "-t", f"refs/vecten/source:{by_commit.subdirectory}", cwd=bare) == "tree"
    )
