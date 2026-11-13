"""Extensions from a git tag or commit, on a laptop and in CI: their record lines, the copies
fetched for a run, and the view of them uv gets.

`requirements.txt` and every lock hold one line per git-sourced extension,
`<name>[extras] @ git+https://<host>/<path>@<full commit>[#subdirectory=<dir>]  # tag <tag>`,
and `extensions.lock.json` records the same origin with the source's `source_sha256`.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from vecten import in_repo
from vecten_shared import git_sources
from vecten_shared.envfile import read_env_values
from vecten_shared.extension_record import GitOrigin
from vecten_shared.git_sources import (
    CREDENTIALS_VARIABLE,
    FORBIDDEN_IN_REF,
    FULL_COMMIT,
    Credentials,
    GitSource,
    parse_source,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import normalize_name

TAG_COMMENT = "  # tag "
# Neither ever reaches uv or a build backend, which run code from the extension's repository.
CREDENTIAL_VARIABLES = frozenset({CREDENTIALS_VARIABLE, "MCP_GIT_CREDENTIALS"})
LINE = re.compile(r"(?P<requirement>\S.*?)(?:\s+#\s*tag\s+(?P<tag>\S+))?\s*")
# A tag that names the version it releases: `v1.2.0`, or `<prefix>/v1.2.0` in a monorepo.
VERSION_TAG = re.compile(r"(?:.+/)?v(?P<version>[^/]+)")


# `<name>[extras] @`, with which a requirement names its package by URL, even a malformed one.
URL_REQUIREMENT = re.compile(r"\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*(?:\[[^\]]*\])?\s*@")


def is_source(text: str) -> bool:
    """Whether a requirement or record line names its package by URL, read toward yes: any
    `git+` or `://` in the requirement counts. A comment, which starts at a `#` at the start
    or after whitespace, is not read."""
    requirement = re.split(r"(?:^|\s)#", text, maxsplit=1)[0].strip()
    if not requirement:
        return False
    if "git+" in requirement or "://" in requirement:
        return True
    try:
        return Requirement(requirement).url is not None
    except InvalidRequirement:
        return URL_REQUIREMENT.match(requirement) is not None


def line_name(line: str) -> str | None:
    """The normalised name a record line installs, or None when it names none."""
    match = LINE.fullmatch(line)
    try:
        return normalize_name(Requirement(match["requirement"]).name) if match else None
    except InvalidRequirement:
        found = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        return normalize_name(found.group()) if found else None


@dataclass(frozen=True)
class GitLine:
    """A record line for a git source pinned to a full commit, and the tag it names."""

    source: GitSource
    tag: str | None

    @property
    def name(self) -> str:
        return normalize_name(self.source.name)

    @property
    def commit(self) -> str:
        return self.source.ref


def parse_line(line: str, where: str) -> GitLine:
    """The pinned git source `line` of the file `where` holds; a refusal never repeats the URL."""
    match = LINE.fullmatch(line)
    name = line_name(line) or "a line"
    try:
        if match is None:
            raise Failure(ExitCode.USAGE, "not a git source")
        source = parse_source(match["requirement"])
    except Failure as failure:
        raise Failure(
            ExitCode.REFUSED,
            f"{where}: {name}: {failure.message}; record the source with `vecten extension add`",
        ) from None
    tag = match["tag"]
    if not FULL_COMMIT.fullmatch(source.ref) or (tag is not None and FORBIDDEN_IN_REF.search(tag)):
        raise Failure(
            ExitCode.REFUSED,
            f"{where}: {name}: a git source is recorded at a full commit with its tag in a "
            "`# tag` comment; record it with `vecten extension add`",
        )
    return GitLine(source, tag)


def source_for(name: str, extras: Iterable[str], origin: GitOrigin) -> GitSource:
    fragment = f"#subdirectory={origin.subdirectory}" if origin.subdirectory else ""
    source = parse_source(f"{name} @ git+{origin.url}@{origin.commit}{fragment}")
    return replace(source, extras=tuple(sorted(extras)))


def line_for(name: str, extras: Iterable[str], origin: GitOrigin) -> str:
    """The line `requirements.txt` and every lock hold for this origin."""
    return source_for(name, extras, origin).requirement_line(origin.commit, origin.tag)


def root_line(name: str, extras: Iterable[str], origin: GitOrigin) -> str:
    """The requirement uv resolves for this origin: the record line without its comment."""
    return source_for(name, extras, origin).requirement_line(origin.commit, None)


def same_source(line: str, origin: GitOrigin) -> bool:
    """Whether `line` (as uv writes it, without a comment) installs exactly `origin`."""
    try:
        source = parse_source(line)
    except Failure:
        return False
    return (source.url, source.ref, source.subdirectory) == (
        origin.url,
        origin.commit,
        origin.subdirectory,
    )


def credentials(root: Path) -> Credentials:
    """`GIT_CREDENTIALS` from the project's `.env`, else from the process environment."""
    env_file = root / ".env"
    value = read_env_values(env_file).get(CREDENTIALS_VARIABLE) if env_file.is_file() else None
    return Credentials.parse(value or os.environ.get(CREDENTIALS_VARIABLE))


@dataclass(frozen=True)
class Copy:
    """A git source fetched for one run: the bare repository holding its commit, the
    extension's directory checked out of it, and what that directory declares."""

    origin: GitOrigin
    repository: Path
    directory: Path
    source_sha256: str
    metadata: in_repo.Metadata


def fetch_copy(
    source: GitSource,
    commit: str,
    tag: str | None,
    public: bool,
    found: Credentials | None,
    workdir: Path,
) -> Copy:
    """Fetch `commit` of `source` into a new directory under `workdir` and check it out.

    The distribution in the checked-out directory must carry the name `source` gives it."""
    base = Path(tempfile.mkdtemp(prefix="source-", dir=workdir))
    repository = git_sources.fetch(source, commit, base / "repository.git", found, tag=tag)
    directory = git_sources.checkout(repository, base / "source", source.subdirectory)
    digest = git_sources.source_sha256(directory)
    metadata = in_repo.read_metadata(directory, f"{source.where}@{commit}")
    wanted = normalize_name(source.name)
    if metadata.name != wanted:
        raise Failure(
            ExitCode.REFUSED,
            f"{source.where} at {commit} holds the distribution {metadata.name}, not {wanted}",
        )
    origin = GitOrigin(source.url, commit, source.subdirectory, tag, public)
    return Copy(origin, repository, directory, digest, metadata)


def check_tag_version(copy: Copy) -> None:
    """Refuse a tag of the form `[<prefix>/]v<version>` whose version is not the package's."""
    match = VERSION_TAG.fullmatch(copy.origin.tag or "")
    if match is None:
        return
    try:
        tagged = Version(match["version"])
        static = Version(copy.metadata.version)
    except InvalidVersion:
        return
    if tagged != static:
        raise Failure(
            ExitCode.REFUSED,
            f"the tag {copy.origin.tag} names version {tagged}, but {copy.metadata.name} at "
            f"{copy.origin.commit} has version {copy.metadata.version}",
        )


def serve_copies(copies: Iterable[Copy], workdir: Path) -> dict[str, Path]:
    """One repository per source URL, under `workdir`, holding every commit fetched from it."""
    by_url: dict[str, list[Copy]] = {}
    for copy in copies:
        by_url.setdefault(copy.origin.url, []).append(copy)
    served = {}
    for url, found in sorted(by_url.items()):
        pairs = sorted({(c.repository, c.origin.commit) for c in found}, key=lambda p: p[1])
        into = Path(tempfile.mkdtemp(prefix="served-", dir=workdir)) / "repository.git"
        served[url] = git_sources.serve(pairs, into)
    return served


def uv_settings(
    base: Mapping[str, str], served: Mapping[str, Path], git_project: bool = False
) -> dict[str, str]:
    """`base` for uv and the build backends it runs: never a git credential, in any project.
    In a project with git sources, also git isolated from the user's configuration, and while
    sources are locked, a git that reads each source URL from the repository `served` names
    for it and reaches nothing else, with GitHub's API left unasked."""
    env = {name: value for name, value in base.items() if name not in CREDENTIAL_VARIABLES}
    if not served and not git_project:
        return env
    env = {
        name: value
        for name, value in env.items()
        if (not name.startswith("GIT_") or name in git_sources.KEPT_GIT_VARIABLES)
        and name not in git_sources.DROPPED_VARIABLES
    }
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    if not served:
        return env
    settings = [git_sources.NO_HELPER]
    for url, repository in sorted(served.items()):
        settings.append((f"url.file://{repository}.insteadOf", url))
    env.update(
        # Every source is rewritten to a local repository, so git never needs another transport.
        GIT_ALLOW_PROTOCOL="file",
        UV_NO_GITHUB_FAST_PATH="1",
        GIT_CONFIG_COUNT=str(len(settings)),
    )
    for i, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env
