"""Extensions from a tag or commit of a git repository reached over HTTPS.

Vecten runs git itself, never through pip or uv, and only with the user's and the system's git
configuration ignored, prompts and traces off, and the credential reachable through one
get-only helper reading a mode-0600 file that exists for a single git command. Nothing in a
URL, a command argument or a message carries a token.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TextIO
from urllib.parse import unquote, urlsplit

from packaging.requirements import InvalidRequirement, Requirement

from vecten_shared.output import ExitCode, Failure

CREDENTIALS_VARIABLE = "GIT_CREDENTIALS"
SOURCE_FORM = (
    "<name>[extras] @ git+https://<host>/<repository>@<tag-or-commit>[#subdirectory=<dir>]"
)
CREDENTIAL_FORM = "https://<username>:<token>@<host>[/<path>]"
# The ref a fetched commit is stored under in the throwaway repository.
SOURCE_REF = "refs/vecten/source"
GIT_TIMEOUT = 300

FULL_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
HEX = re.compile(r"[0-9a-fA-F]{4,63}")
# git check-ref-format's rules, plus no leading `-` so a ref never reads as an option and no
# `%` so the URL needs no decoding.
FORBIDDEN_IN_REF = re.compile(r"[\x00-\x20\x7f~^:?*\[\\%]|\.\.|@\{|//|^[-/.]|[/.]$|\.lock$|/\.")
SEGMENT = re.compile(r"[A-Za-z0-9._~-]+")

# Variables git reads that may stay: the trust store a proxy that re-signs HTTPS needs.
KEPT_GIT_VARIABLES = frozenset({"GIT_SSL_CAINFO", "GIT_SSL_CAPATH"})
# libcurl reads `NETRC`, or `.netrc` in `HOME`, which each run replaces with an empty directory.
DROPPED_VARIABLES = frozenset({"SSH_ASKPASS", "NETRC"})

USER_INFORMATION = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
AUTHORIZATION = re.compile(r"(?im)\b((?:proxy-)?authorization:[ \t]*)[^\r\n]+")
MASK = "***"


def _usage(message: str) -> Failure:
    return Failure(ExitCode.USAGE, message)


@dataclass(frozen=True)
class GitSource:
    """A requirement on a tag or commit of a git repository, as `extension add` names it."""

    name: str
    extras: tuple[str, ...]
    host: str
    repository: str
    ref: str
    subdirectory: str | None

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.repository}"

    @property
    def where(self) -> str:
        return f"{self.host}/{self.repository}"

    def requirement_line(self, commit: str, tag: str | None) -> str:
        """The line `requirements.txt` and every lock hold for this source at `commit`."""
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        fragment = f"#subdirectory={self.subdirectory}" if self.subdirectory else ""
        line = f"{self.name}{extras} @ git+{self.url}@{commit}{fragment}"
        return f"{line}  # tag {tag}" if tag else line


def _subdirectory(fragment: str) -> str | None:
    if not fragment:
        return None
    key, equals, value = fragment.partition("=")
    relative = value.rstrip("/")
    if (
        key != "subdirectory"
        or not equals
        or not relative
        or value.startswith("/")
        or any(
            segment in (".", "..") or not SEGMENT.fullmatch(segment)
            for segment in relative.split("/")
        )
    ):
        raise _usage(
            "the only fragment a git source may carry is #subdirectory=<dir>, a relative "
            "directory inside the repository"
        )
    return relative


def parse_source(text: str) -> GitSource:
    """The git source `text` names; a refusal never repeats the URL, which may hold a token."""
    if any(c in text for c in "\r\n"):
        raise _usage(f"a git source is one line: {SOURCE_FORM}")
    try:
        parsed = Requirement(text)
    except InvalidRequirement:
        raise _usage(f"not a git source; use {SOURCE_FORM}") from None
    if parsed.url is None:
        raise _usage(f"not a git source; use {SOURCE_FORM}")
    if parsed.marker is not None:
        raise _usage("a git source takes no environment markers")
    try:
        parts = urlsplit(parsed.url)
        port_ok = parts.port is None or parts.port > 0
    except ValueError:
        raise _usage(f"not a git source; use {SOURCE_FORM}") from None
    if parts.scheme != "git+https":
        raise _usage(
            "a git source must use git+https; ssh, http, file URLs and paths are not supported"
        )
    if "@" in parts.netloc:
        raise _usage(
            f"the URL carries credentials; remove them and put them in {CREDENTIALS_VARIABLE}"
        )
    if parts.query:
        raise _usage("a git source's URL takes no query")
    if not parts.hostname or not port_ok:
        raise _usage(f"a git source names a host: {SOURCE_FORM}")
    path, at, ref = parts.path.rpartition("@")
    if not at or not ref:
        raise _usage(f"name a tag or a full commit after the repository: {SOURCE_FORM}")
    if FORBIDDEN_IN_REF.search(ref):
        raise _usage("the ref is not a valid tag name or commit")
    segments = path.strip("/").split("/")
    if not path.strip("/") or any(
        segment in (".", "..") or not SEGMENT.fullmatch(segment) for segment in segments
    ):
        raise _usage("the repository path is not a plain path of names")
    commit = ref.lower()
    return GitSource(
        name=parsed.name,
        extras=tuple(sorted(parsed.extras)),
        host=parts.netloc.lower(),
        repository="/".join(segments),
        ref=commit if FULL_COMMIT.fullmatch(commit) else ref,
        subdirectory=_subdirectory(parts.fragment),
    )


def _repository_key(path: str) -> str:
    path = path.strip("/")
    return path.removesuffix(".git")


@dataclass(frozen=True)
class Credential:
    """One `GIT_CREDENTIALS` entry; `path` is empty for an entry that covers its whole host."""

    host: str
    path: str
    username: str
    entry: str = field(repr=False)
    token: str = field(repr=False)
    # The username and token as the entry spells them, percent-encoding kept, which is the
    # form git's credential store reads.
    raw_username: str = field(repr=False)
    raw_token: str = field(repr=False)

    def covers(self, host: str, repository: str) -> bool:
        if self.host != host.lower():
            return False
        if not self.path:
            return True
        mine, theirs = _repository_key(self.path), _repository_key(repository)
        return theirs == mine or theirs.startswith(f"{mine}/")

    def store_line(self, repository: str) -> str:
        """The credential-store line answering for `repository` alone, or for the host when
        the entry has no path."""
        where = f"{self.host}/{repository}" if self.path else self.host
        return f"https://{self.raw_username}:{self.raw_token}@{where}\n"


def _entry(position: int, raw: str) -> Credential:
    refused = Failure(
        ExitCode.CONFIG,
        f"{CREDENTIALS_VARIABLE} entry {position} is not {CREDENTIAL_FORM}; entries are "
        "separated by whitespace",
    )
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError:
        raise refused from None
    userinfo, at, host = parts.netloc.rpartition("@")
    username, colon, token = userinfo.partition(":")
    if (
        parts.scheme != "https"
        or not at
        or not colon
        or not username
        or not token
        or not host
        or parts.query
        or parts.fragment
        or raw.endswith(("?", "#"))
    ):
        raise refused
    return Credential(
        host=host.lower(),
        path=parts.path.strip("/"),
        username=unquote(username),
        entry=raw,
        token=unquote(token),
        raw_username=username,
        raw_token=token,
    )


@dataclass(frozen=True)
class Credentials:
    """The entries of `GIT_CREDENTIALS`, from a laptop's `.env` or a CI step's environment."""

    entries: tuple[Credential, ...] = ()

    @classmethod
    def parse(cls, value: str | None) -> Credentials:
        return cls(tuple(_entry(i, raw) for i, raw in enumerate((value or "").split(), 1)))

    def for_repository(self, host: str, repository: str) -> Credential | None:
        """The entry with the longest path covering `repository` on `host`; the first of
        equals."""
        best: Credential | None = None
        for entry in self.entries:
            if entry.covers(host, repository) and (
                best is None or len(_repository_key(entry.path)) > len(_repository_key(best.path))
            ):
                best = entry
        return best

    def _secrets(self) -> list[str]:
        found = {
            secret
            for entry in self.entries
            for secret in (entry.entry, entry.token, entry.raw_token)
            if secret
        }
        return sorted(found, key=len, reverse=True)

    def mask(self, text: str) -> str:
        """`text` without any token, entry, URL user information or Authorization value."""
        for secret in self._secrets():
            text = text.replace(secret, MASK)
        text = USER_INFORMATION.sub(rf"\g<1>{MASK}@", text)
        return AUTHORIZATION.sub(rf"\g<1>{MASK}", text)

    def announce(self, environ: Mapping[str, str] | None = None, out: TextIO | None = None) -> None:
        """Register every token with GitHub's log masking when running in GitHub Actions."""
        environ = os.environ if environ is None else environ
        if environ.get("GITHUB_ACTIONS") != "true":
            return
        out = sys.stdout if out is None else out
        seen: set[str] = set()
        for entry in self.entries:
            for token in (entry.raw_token, entry.token):
                if token not in seen:
                    seen.add(token)
                    out.write(f"::add-mask::{token}\n")
        out.flush()


def git_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """`base` without any variable git would read, plus the settings that isolate git."""
    base = os.environ if base is None else base
    env = {
        name: value
        for name, value in base.items()
        if (not name.startswith("GIT_") or name in KEPT_GIT_VARIABLES)
        and name not in DROPPED_VARIABLES
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="https",
        # Failures are told apart by git's English messages.
        LC_ALL="C",
        LANGUAGE="C",
    )
    return env


@dataclass(frozen=True)
class GitRun:
    """The environment for one git command, and the credential file it reads, if any."""

    env: dict[str, str]
    credential_file: Path | None
    credential: Credential | None


def _with_config(env: dict[str, str], settings: list[tuple[str, str]]) -> dict[str, str]:
    env = dict(env)
    env["GIT_CONFIG_COUNT"] = str(len(settings))
    for i, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


# An empty value clears every helper configured before it, Apple's system one included.
NO_HELPER = ("credential.helper", "")


@contextmanager
def credential_environment(source: GitSource, credentials: Credentials | None) -> Iterator[GitRun]:
    """The isolated environment for git on `source`, with the matching credential, if any,
    in a file that is deleted when the block ends."""
    entry = credentials.for_repository(source.host, source.repository) if credentials else None
    with tempfile.TemporaryDirectory(prefix="vecten-git-") as directory:
        home = Path(directory) / "home"
        home.mkdir()
        base = {**git_environment(), "HOME": str(home)}
        if entry is None:
            yield GitRun(_with_config(base, [NO_HELPER]), None, None)
            return
        stored = Path(directory) / "credentials"
        descriptor = os.open(stored, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(entry.store_line(source.repository))
        # git appends the action to the helper; only `get` reaches the store, so a refused
        # token is never erased from it and an accepted one never written back.
        helper = (
            f'!f() {{ test "$1" = get && exec git credential-store '
            f"--file={shlex.quote(str(stored))} get; }}; f"
        )
        settings = [NO_HELPER, ("credential.helper", helper)]
        if entry.path:
            settings.append(("credential.useHttpPath", "true"))
        try:
            yield GitRun(_with_config(base, settings), stored, entry)
        finally:
            stored.unlink(missing_ok=True)


def _local() -> GitRun:
    """The isolated environment for a git command that reaches no remote."""
    return GitRun(_with_config(git_environment(), [NO_HELPER]), None, None)


class Cause(StrEnum):
    AUTHENTICATION = "authentication refused"
    REPOSITORY = "repository not found or not visible"
    REF = "ref not found"
    COMMIT = "commit not reachable"
    BRANCH = "branch or ambiguous ref"
    NETWORK = "network failure"


CAUSE_CODES = {
    Cause.AUTHENTICATION: ExitCode.PREREQUISITE,
    Cause.REPOSITORY: ExitCode.PREREQUISITE,
    Cause.REF: ExitCode.USAGE,
    Cause.BRANCH: ExitCode.USAGE,
    Cause.COMMIT: ExitCode.REFUSED,
    Cause.NETWORK: ExitCode.TOOL_FAILED,
}

AUTHENTICATION_SIGNS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "unable to get password",
    "terminal prompts disabled",
    "returned error: 401",
    "returned error: 403",
    "invalid username or",
    "access denied",
)
REPOSITORY_SIGNS = ("not found", "returned error: 404", "does not appear to be a git repository")
COMMIT_SIGNS = (
    "not our ref",
    "unadvertised object",
    "couldn't find remote ref",
    "no such remote ref",
)


class GitSourceFailure(Failure):
    """A git command on a source failed for `cause`; the message names the host, the
    repository and the ref, and shows git's output only masked."""

    def __init__(self, source: GitSource, ref: str, cause: Cause, hint: str, output: str = ""):
        message = f"{source.where} at {ref}: {cause.value}; {hint}"
        if output:
            message += "\n" + "\n".join(f"  git: {line}" for line in output.splitlines())
        super().__init__(
            CAUSE_CODES[cause],
            message,
            {
                "host": source.host,
                "repository": source.repository,
                "ref": ref,
                "cause": cause.value,
            },
        )
        self.cause = cause


def _classify(stderr: str) -> Cause:
    text = stderr.lower()
    if any(sign in text for sign in COMMIT_SIGNS):
        return Cause.COMMIT
    if any(sign in text for sign in AUTHENTICATION_SIGNS):
        return Cause.AUTHENTICATION
    if any(sign in text for sign in REPOSITORY_SIGNS):
        return Cause.REPOSITORY
    return Cause.NETWORK


def _git(args: list[str], run: GitRun, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=run.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise Failure(ExitCode.PREREQUISITE, "git is not installed; install git") from None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, "", f"timed out after {GIT_TIMEOUT}s")


def _hint(cause: Cause, source: GitSource, entry: Credential | None) -> str:
    if cause is Cause.AUTHENTICATION:
        if entry is None:
            return (
                f"set {CREDENTIALS_VARIABLE} with an entry for https://{source.host}, or for "
                "the organisation's path on it, whose token can read the repository"
            )
        covered = f"https://{entry.host}/{entry.path}" if entry.path else f"https://{entry.host}"
        return f"the {CREDENTIALS_VARIABLE} entry for {covered} was refused; replace its token"
    if cause is Cause.REPOSITORY:
        return f"check the repository path, and that the {CREDENTIALS_VARIABLE} token can read it"
    if cause is Cause.COMMIT:
        return "the host no longer serves it; record a reachable commit with `vecten extension add`"
    return "check the network connection and the host name"


def _failure(
    source: GitSource,
    ref: str,
    result: subprocess.CompletedProcess,
    run: GitRun,
    credentials: Credentials | None,
) -> GitSourceFailure:
    cause = _classify(result.stderr)
    # A classified cause says all git did; any other failure shows git's own words.
    output = ""
    if cause is Cause.NETWORK:
        output = (credentials or Credentials()).mask(result.stderr.strip())
    return GitSourceFailure(source, ref, cause, _hint(cause, source, run.credential), output)


@dataclass(frozen=True)
class Resolution:
    """The commit a ref names; `public` when the host answered without a credential."""

    commit: str
    tag: str | None
    public: bool


def _ls_remote(
    source: GitSource, patterns: list[str], credentials: Credentials | None
) -> tuple[dict[str, str], bool]:
    """The refs matching `patterns`, and whether they were read without a credential.

    The repository is asked anonymously first, so a public one is recorded as public even
    when `GIT_CREDENTIALS` covers its host."""
    args = ["ls-remote", source.url, *patterns]
    with credential_environment(source, None) as anonymous:
        result = _git(args, anonymous)
    public = True
    if result.returncode != 0 and _classify(result.stderr) is Cause.AUTHENTICATION:
        with credential_environment(source, credentials) as run:
            if run.credential is not None:
                result = _git(args, run)
                public = False
            if result.returncode != 0:
                raise _failure(source, source.ref, result, run, credentials)
    elif result.returncode != 0:
        raise _failure(source, source.ref, result, anonymous, credentials)
    refs = {}
    for line in result.stdout.splitlines():
        oid, _, name = line.partition("\t")
        refs[name] = oid
    return refs, public


def resolve(source: GitSource, credentials: Credentials | None) -> Resolution:
    """The commit `source.ref` names: a full commit as given, or an exact tag, peeled.

    A branch, a name that is both a tag and a branch, a missing ref and an abbreviated commit
    are refused."""
    if credentials:
        credentials.announce()
    ref = source.ref
    if FULL_COMMIT.fullmatch(ref):
        _, public = _ls_remote(source, ["HEAD"], credentials)
        return Resolution(ref, None, public)
    refs, public = _ls_remote(
        source, [f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}"], credentials
    )
    tag = refs.get(f"refs/tags/{ref}^{{}}") or refs.get(f"refs/tags/{ref}")
    branch = refs.get(f"refs/heads/{ref}")
    if tag and branch:
        raise GitSourceFailure(
            source, ref, Cause.BRANCH, "it is both a tag and a branch; name a full commit"
        )
    if branch:
        raise GitSourceFailure(
            source,
            ref,
            Cause.BRANCH,
            "it is a branch, and a branch does not pin a version; name a tag or a full commit",
        )
    if tag:
        return Resolution(tag, ref, public)
    if HEX.fullmatch(ref):
        hint = (
            "there is no tag of that name, and an abbreviated commit is not accepted; "
            "give the full commit"
        )
    else:
        hint = "there is no tag of that name in the repository"
    raise GitSourceFailure(source, ref, Cause.REF, hint)


def fetch(
    source: GitSource,
    commit: str,
    into: Path,
    credentials: Credentials | None,
    tag: str | None = None,
) -> Path:
    """A bare repository at `into` holding `commit` alone under `SOURCE_REF`.

    Where the host refuses to fetch by hash, `tag` is fetched instead and must still point at
    `commit`."""
    if not FULL_COMMIT.fullmatch(commit) or (tag is not None and FORBIDDEN_IN_REF.search(tag)):
        raise _usage(
            f"{source.where}: the recorded commit or tag is malformed; "
            "record the source again with `vecten extension add`"
        )
    if credentials:
        credentials.announce()
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    init = _git(["init", "--quiet", "--bare", "--template=", str(into)], _local())
    if init.returncode != 0:
        raise Failure(ExitCode.TOOL_FAILED, f"git could not create a repository in {into}")
    shown = f"{commit} (tag {tag})" if tag else commit
    fetch_args = ["fetch", "--quiet", "--depth", "1", "--no-tags", source.url]
    with credential_environment(source, credentials) as run:
        result = _git([*fetch_args, f"+{commit}:{SOURCE_REF}"], run, cwd=into)
        if result.returncode != 0 and tag and _classify(result.stderr) is Cause.COMMIT:
            tag_ref = f"refs/tags/{tag}"
            result = _git([*fetch_args, f"+{tag_ref}:{tag_ref}"], run, cwd=into)
            if result.returncode == 0:
                _adopt_tag(source, commit, tag, into, shown)
        if result.returncode != 0:
            raise _failure(source, shown, result, run, credentials)
    if _peel(into, SOURCE_REF) != commit:
        raise GitSourceFailure(source, shown, Cause.COMMIT, _hint(Cause.COMMIT, source, None))
    return into


def _peel(into: Path, ref: str) -> str:
    result = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], _local(), cwd=into)
    return result.stdout.strip()


def _adopt_tag(source: GitSource, commit: str, tag: str, into: Path, shown: str) -> None:
    """Store the fetched tag's commit under `SOURCE_REF`, refusing a tag that moved."""
    tag_ref = f"refs/tags/{tag}"
    found = _peel(into, tag_ref)
    if found != commit:
        raise GitSourceFailure(
            source,
            shown,
            Cause.COMMIT,
            f"the host refuses to fetch by commit and the tag {tag} now points at {found}; "
            "record a reachable commit with `vecten extension add`",
        )
    _git(["update-ref", SOURCE_REF, commit], _local(), cwd=into)
    _git(["update-ref", "-d", tag_ref], _local(), cwd=into)
