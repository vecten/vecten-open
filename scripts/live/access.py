#!/usr/bin/env python3
"""Run live-check commands with the test credentials, once every tool proves its identity."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import NoReturn

ALLOWED_EXECUTABLES = frozenset({"vecten", "aws", "scw", "snow", "gh", "git", "terraform", "uv"})
IDENTITIES = ("aws", "scaleway", "snowflake", "github")
GITHUB_MODES = ("sandbox", "upstream")
# The AWS profile and the Snowflake connection the credential home defines.
LIVECHECK = "livecheck"
PROBE_TIMEOUT = 30.0
TOKEN_COMMAND = [sys.executable, "-I", str(Path(__file__).with_name("github_app.py")), "token"]
PROBES = {
    name: shlex.split(command)
    for name, command in {
        "aws": "aws sts get-caller-identity --query Account --output text",
        "scaleway": "scw account project list -o json",
        "snowflake": f'snow sql -c {LIVECHECK} -q "select current_account()" --format json',
        "github": 'gh api /installation/repositories --paginate --jq ".repositories[].full_name"',
    }.items()
}
PASSED = frozenset(
    "PATH LANG TERM TMPDIR USER LOGNAME SHELL TZ SSL_CERT_FILE SSL_CERT_DIR HTTP_PROXY HTTPS_PROXY"
    " NO_PROXY ALL_PROXY http_proxy https_proxy no_proxy all_proxy".split()
)
GH_UNCONFIGURED = "livecheck-github-not-configured"
# The scripts beside this one that the wrapper runs by name.
SIBLINGS = {
    "combos": "combos.py",
    "cost": "cost.py",
    "candidate": "candidate.py",
    "project": "sandbox_project.py",
}
# The check must see at least what a push sends and `git add --all` would stage. A push sends
# the original objects, not what `refs/replace/` substitutes, and the push runs with another
# home, so only the project's own ignore files (.gitignore, .git/info/exclude, which `add`
# honours too) may hide a file; and reading the index would run any configured fsmonitor.
GIT = [
    "git",
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.excludesFile={os.devnull}",
]
# Where a push can publish a file from: `vecten github push` stages everything `git add --all`
# would, and a push sends every commit its refs reach. Keys as the secret check's JSON gives them.
PLACES = {"working-tree": "in the working tree", "staged": "staged", "commit": "in a commit"}
REWRITE = "removing the file is not enough: rewrite the history or render the project afresh"
# The secret check's findings for the public check, which cannot start a sandbox inside its own;
# a sibling's environment comes from an allowlist, so only the wrapper can set it.
SECRET_HITS = "VECTEN_LIVE_SECRET_HITS"
# Code planted in the interpreter would run in the next wrapper before any of its checks.
INTERPRETER = (sys.base_prefix, sys.prefix)
OUTSIDE_TOOLS = (
    "is inside the tools copy {root}, which is not a place to work in;"
    " run from the working checkout or a sandbox project"
)
PLATFORM = sys.platform
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# The maintainer's own credential files under the real home: (Seatbelt filter, path).
DENIED = (
    *(
        ("subpath", path)
        for path in (
            ".aws", ".config/scw", ".snowflake", ".config/snowflake", ".ssh", ".config/gh",
            ".gnupg", ".kube", ".config/gcloud", ".terraform.d", "Library/Keychains",
            "Library/Application Support/snowflake", ".dbt", ".snowsql", ".config/op",
        )
    ),
    *(
        ("literal", path)
        for path in (
            ".docker/config.json", ".netrc", ".pypirc", ".npmrc", ".git-credentials",
            ".config/git/credentials", ".terraformrc", ".claude/.credentials.json", ".pgpass",
        )
    ),
)  # fmt: skip
# The sandbox projects under the real home, whose `.env` files `vecten` reads.
SANDBOX_PROJECTS = "vecten-sandbox"
# A Seatbelt regex for a `.env` or `.env.<name>` file anywhere under the real home, whose path
# is spliced in as `home` with REGEX_SPECIAL escaped.
ENV_FILES = r"^{home}/(.*/)?\.env(\.[^/]+)?$"
REGEX_SPECIAL = frozenset(".^$|?*+()[]{}")
# What the wrapped tools write in the credential home; the rest of it, `allow.toml` and the
# token cache above all, is the guard's own and stays unwritable.
LIVE_WRITABLE = ("home", "home-plain", "snowflake/logs", "github/gh")
# Files that steer every later shell or git under the real home, and the trees of the agent's
# own settings, hooks, plugins and memory.
HOME_UNWRITABLE = (
    ".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile", ".gitconfig",
)  # fmt: skip
HOME_UNWRITABLE_TREES = (".config/git", ".claude")
# A Seatbelt regex for any directory named `.claude`, its settings files and the trees whose
# scripts and prompts the agent runs. It matches by name anywhere, so a checkout moved out of
# the home and back stays covered, and it matches the directory and each named entry too, so
# none can be renamed away and back.
CLAUDE_CONTROL = (
    r"^(.*/)?\.claude"
    r"(/(settings\.json|settings\.local\.json|hooks|skills|commands|agents)(/.*)?)?$"
)
# Any unix socket may belong to an agent or daemon that acts with the maintainer's access
# (Docker, ssh-agent, gpg-agent), so every one is denied and only the resolver's is open.
ALLOWED_SOCKETS = ("/private/var/run/mDNSResponder",)
# A run's TMPDIR outlives it, since the wrapper becomes the command; the next run clears any
# older than a day.
TMP_KEPT = 86400
# The commands' own HOME holds tool caches and binaries, not the credentials the home was given.
NOT_SECRET = (
    ("allow.toml",), ("google", "hostnames.txt"), ("google", "oidc-client-id"),
    ("snowflake", "logs"), ("github", "gh"), ("git", "config"), ("home",), ("home-plain",),
    ("google", "admin.toml"),
)  # fmt: skip
SSH_REFUSED = "sh -c 'echo \"access: git over SSH is refused; use https://\" >&2; exit 1'"
MIN_SECRET = 16
MIN_REDACTED = 8
# These keys name things (an account, a user, a place, a time) and grant no access, so their
# values are not secrets.
IDENTIFIER_KEYS = frozenset(
    "account accountname user username role warehouse database schema host region default_region"
    " default_zone default_organization_id default_project_id authenticator private_key_file"
    " private_key_path expires_at app_id installation_id output".split()
)
QUOTED = re.compile(r"""(["'])(.*?)\1""")
ASSIGNED = re.compile(r"^([\w.-]+)\s*[=:]\s*([^=\s].*)$")
Found = dict[str, str | list[str] | None]


class Refused(Exception):
    pass


def credential_home() -> Path:
    override = os.environ.get("VECTEN_LIVE_HOME")
    return Path(override) if override else Path.home() / ".config" / "vecten-livecheck"


def tools_root() -> Path:
    """The repository holding this file: the tools copy, which only the maintainer updates."""
    return Path(os.path.abspath(__file__)).parents[2]


def _inside_tools(directory: Path) -> bool:
    return directory.resolve().is_relative_to(tools_root().resolve())


def real_home() -> Path:
    """The account's own home directory; HOME can name any directory."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _own_directory(path: Path) -> None:
    """Make `path` a directory only its owner can enter; refuse a link or file planted there."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise Refused(f"{path} is not a directory")


def tool_env(
    home: Path, base: Mapping[str, str], github_token: str, credentials: bool = True
) -> dict[str, str]:
    # Any variable a tool reads that the wrapper did not set is a way around the credential
    # home, so only identity-free variables pass, and HOME moves so default lookups miss.
    env = {key: value for key, value in base.items() if key in PASSED or key.startswith("LC_")}
    # Runs with and without a credential keep apart homes, so neither's tools find the other's
    # state where they look by default.
    own_home = home / ("home" if credentials else "home-plain")
    if home.is_dir():
        _own_directory(own_home)
    git_global = home / "git/config"
    # Git asks every configured helper in turn and takes the first answer; the empty helper
    # empties the list, so no helper but gh's is asked for github.com.
    git = [("credential.https://github.com.helper", "")]
    if github_token:
        git.append(("credential.https://github.com.helper", "!gh auth git-credential"))
    git += [
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
    ]
    env |= {
        "HOME": str(own_home),
        "GH_CONFIG_DIR": str(home / "github/gh"),
        # Without a token gh answers from the macOS keyring, whatever GH_CONFIG_DIR says.
        "GH_TOKEN": github_token or GH_UNCONFIGURED,
        "DOCKER_CONFIG": str(home / "docker"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(git_global) if git_global.is_file() else os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        # A promisor remote would otherwise be fetched from, by a program it names.
        "GIT_NO_LAZY_FETCH": "1",
        # A remote left on SSH would authenticate with the maintainer's key.
        "GIT_SSH_COMMAND": SSH_REFUSED,
        "UV_CACHE_DIR": str(real_home() / ".cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(real_home() / ".local/share/uv/python"),
        "GIT_CONFIG_COUNT": str(len(git)),
    }
    if credentials:
        env |= {
            "AWS_CONFIG_FILE": str(home / "aws/config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(home / "aws/credentials"),
            "AWS_PROFILE": LIVECHECK,
            "SCW_CONFIG_PATH": str(home / "scw/config.yaml"),
            "SNOWFLAKE_HOME": str(home / "snowflake"),
            "SNOWFLAKE_DEFAULT_CONNECTION_NAME": LIVECHECK,
            "VECTEN_LIVE": "1",
        }
    for index, (key, value) in enumerate(git):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def _variants(path: Path) -> list[str]:
    # Seatbelt matches resolved paths; a symlinked path is denied at both ends.
    variants = list(dict.fromkeys((str(path), str(path.resolve()))))
    for variant in variants:
        if '"' in variant or "\\" in variant:
            raise Refused(f"no sandbox rule can name the path {variant!r}")
    return variants


def _filters(rules: Iterable[tuple[str, Path]]) -> str:
    return " ".join(f'({kind} "{v}")' for kind, path in rules for v in _variants(path))


def env_files_pattern(home: str) -> str:
    return ENV_FILES.format(home="".join(f"\\{c}" if c in REGEX_SPECIAL else c for c in home))


def _ancestors(rules: Iterable[tuple[str, Path]]) -> list[str]:
    """Every directory above a denied path, as written and resolved, short of the root.

    A write deny names a path, so renaming a directory above it, writing beneath the new name
    and renaming it back would plant a file there; denying the rename of each ancestor stops
    that.
    """
    found = (
        str(parent)
        for _, path in rules
        for variant in _variants(path)
        for parent in Path(variant).parents
        if parent.parent != parent
    )
    return list(dict.fromkeys(found))


def sandbox_profile(
    account_home: Path,
    live_home: Path,
    tools: Path,
    interpreter: Iterable[Path],
    sockets: Path | None = None,
) -> str:
    """A Seatbelt profile that keeps tools, and slips, off the maintainer's credentials.

    It allows everything by default and denies what it names, so it does not contain a process
    that sets out to get out: such a process can still write most of the home directory, read
    any credential file the profile does not name, and start processes outside the profile
    through LaunchServices, Apple Events or launchd.
    """
    homes = _variants(account_home)
    env_files = " ".join(f'(regex #"{env_files_pattern(v)}")' for v in homes)
    claude = f'(regex #"{CLAUDE_CONTROL}")'
    credentials = [(kind, account_home / relative) for kind, relative in DENIED]
    guarded = [("subpath", live_home)]
    unwritable = [
        *(("literal", account_home / relative) for relative in HOME_UNWRITABLE),
        *(("subpath", account_home / relative) for relative in HOME_UNWRITABLE_TREES),
        ("subpath", tools),
        *(("subpath", Path(p)) for p in dict.fromkeys(map(str, interpreter))),
    ]
    ancestors = _ancestors([*credentials, *guarded, *unwritable])
    unlinks = " ".join(f'(literal "{path}")' for path in ancestors)
    # The later rule wins, so each allow follows the deny it narrows and no file rule follows the
    # last two denies: the steering files, the tools copy and its interpreter stay unwritable.
    opened = "".join(f'(allow network-outbound (literal "{path}"))' for path in ALLOWED_SOCKETS)
    if sockets is not None:
        opened += _own_sockets(sockets)
    return (
        "(version 1)(allow default)"
        f"(deny file-read* {env_files})"
        f"(allow file-read* {_filters([('subpath', account_home / SANDBOX_PROJECTS)])})"
        f"(deny file-read* file-write* {_filters(credentials)})"
        f"(deny file-write* {_filters(guarded)})"
        f"(allow file-write* {_filters(('subpath', live_home / p) for p in LIVE_WRITABLE)})"
        f"(deny file-write* {_filters(unwritable)} {claude})"
        f"(deny file-write-unlink {unlinks})"
        f"(deny network-outbound (remote unix-socket)){opened}"
    )


def _own_sockets(directory: Path) -> str:
    """Rules opening the sockets a run binds directly in its own fresh directory.

    A daemon's socket could otherwise be moved in, alone or in its directory, or the
    directory swapped for the daemon's: no socket elsewhere can be unlinked (a move unlinks
    its source), one a level down is not opened, and neither the directory nor one above it
    can be moved.
    """
    entries = " ".join(
        f'(regex #"^{"".join(f"\\{c}" if c in REGEX_SPECIAL else c for c in v)}/[^/]+$")'
        for v in _variants(directory)
    )
    fixed = " ".join(
        f'(literal "{path}")'
        for path in dict.fromkeys([*_variants(directory), *_ancestors([("subpath", directory)])])
    )
    return (
        f"(allow network-outbound {entries})"
        "(deny file-write-unlink (vnode-type SOCKET))"
        f"(allow file-write-unlink {entries})"
        f"(deny file-write-unlink {fixed})"
    )


def _os_sandbox(sockets: Path | None = None) -> list[str]:
    """The argv prefix that runs a command under the deny profile; empty off macOS."""
    if PLATFORM != "darwin":
        return []
    interpreter = [Path(prefix).resolve() for prefix in INTERPRETER]
    profile = sandbox_profile(real_home(), credential_home(), tools_root(), interpreter, sockets)
    return [SANDBOX_EXEC, "-p", profile]


def _private_tmp(home: Path) -> Path:
    """A new directory only this run uses, for TMPDIR: tools that talk to their own helpers
    over a unix socket (Terraform and its providers) make it there."""
    parent = home / "home" / "tmp"
    _own_directory(home / "home")
    _own_directory(parent)
    stale = time.time() - TMP_KEPT
    for entry in parent.iterdir():
        if os.lstat(entry).st_mtime < stale:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    return Path(tempfile.mkdtemp(dir=parent))


def plain_env() -> dict[str, str]:
    """The environment of a tool run on the agent's files with no credential at all."""
    return tool_env(credential_home(), os.environ, "", credentials=False)


def _problem(name: str, allowed: dict, value: str | list[str], github: str) -> str | None:
    if name == "aws" and value != str(allowed.get("account_id")):
        return f"account {value} is not the live-check account"
    if name == "scaleway":
        others = sorted(set(value) - {str(allowed.get("organization_id"))})
        if others:
            return f"organization {', '.join(others)} is not the live-check organization"
    # Snowflake account locators are case-insensitive.
    if name == "snowflake" and value.upper() != str(allowed.get("account")).upper():
        return f"account {value} is not the live-check account"
    if name == "github" and github == "sandbox":
        org = allowed.get("sandbox_org")
        outside = [repo for repo in value if not repo.startswith(f"{org}/")]
        if outside:
            return f"the sandbox token reaches {', '.join(outside)}, outside {org}"
    if name == "github" and github == "upstream":
        repo = allowed.get("upstream_repo")
        if value != [repo]:
            reached = ", ".join(value) or "no repository"
            return f"the upstream token reaches {reached}, not exactly {repo}"
    return None


def violations(allow: dict, found: Found, github: str) -> list[str]:
    problems = []
    for name in IDENTITIES:
        if name not in allow:
            continue
        value = found.get(name)
        problem = (
            "identity could not be read"
            if value is None
            else _problem(name, allow[name], value, github)
        )
        if problem:
            problems.append(f"{name}: {problem}")
    return problems


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted((s for s in secrets if len(s) >= MIN_REDACTED), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _skipped(home: Path, path: Path) -> bool:
    parts = path.relative_to(home).parts
    return any(parts[: len(prefix)] == prefix for prefix in NOT_SECRET)


def _read_regular(path: Path) -> str | None:
    """The text of `path` when it is a regular file itself; wrapped commands can plant links."""
    try:
        # Non-blocking, so a planted pipe cannot hold the open.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        return handle.read().decode(errors="ignore")


def secret_values(home: Path) -> set[str]:
    values = set()
    # os.walk lists a linked directory without entering it.
    for directory, subdirectories, names in os.walk(home):
        here = Path(directory)
        subdirectories[:] = [name for name in subdirectories if not _skipped(home, here / name)]
        for path in (here / name for name in names):
            if not _skipped(home, path):
                values |= _secrets_in(path)
    return values


def _secrets_in_json(data: object) -> set[str]:
    if isinstance(data, dict):
        values = set()
        for key, value in data.items():
            if str(key).lower() not in IDENTIFIER_KEYS:
                values |= _secrets_in_json(value)
        return values
    if isinstance(data, list):
        return set().union(*map(_secrets_in_json, data))
    value = str(data)
    return {value} if len(value) >= MIN_SECRET else set()


def _secrets_in(path: Path) -> set[str]:
    values = set()
    text = _read_regular(path)
    if text is None:
        return values
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        return _secrets_in_json(data)
    for line in (raw.strip() for raw in text.splitlines()):
        if line.startswith("[") and line.endswith("]"):
            continue
        if assigned := ASSIGNED.match(line):
            if assigned.group(1).lower() in IDENTIFIER_KEYS:
                continue
            candidates = [assigned.group(2).strip().strip("\"'")]
        else:
            candidates = [line, *(match.group(2) for match in QUOTED.finditer(line))]
        values.update(value for value in candidates if len(value) >= MIN_SECRET)
    return values


def _text(out: str) -> str:
    value = out.strip()
    if not value or "\n" in value:
        raise ValueError(out)
    return value


def _organizations(out: str) -> list[str]:
    organizations = [str(project["organization_id"]) for project in json.loads(out)]
    if not organizations:
        raise ValueError(out)
    return organizations


def _snowflake_account(out: str) -> str:
    rows = json.loads(out)
    if rows and isinstance(rows[0], list):
        rows = rows[0]
    return _text(str(next(iter(rows[0].values()))))


def _repositories(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines() if line.strip()]


PARSERS: dict[str, Callable[[str], str | list[str]]] = {
    "aws": _text,
    "scaleway": _organizations,
    "snowflake": _snowflake_account,
    "github": _repositories,
}


def _probe(what, argv, env, parse, secrets) -> tuple[str | list[str] | None, str | None]:
    """Return the parsed answer, or None and a redacted reason when it cannot be told."""
    try:
        # Whatever the caller pipes in is the command's, so no probe may read it first.
        done = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PROBE_TIMEOUT,
        )
    except OSError as missing:
        return None, f"{what} could not start ({missing.strerror})"
    except subprocess.TimeoutExpired:
        return None, f"{what} gave no answer within {PROBE_TIMEOUT:g} seconds"
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines() or ["no message"]
        return None, redact(f"{what} exited {done.returncode}: {lines[-1]}", secrets)
    try:
        return parse(done.stdout), None
    except ValueError, LookupError, TypeError, AttributeError, StopIteration:
        return None, f"the output of {what} could not be read"


def _examine(
    home: Path,
    allow: dict,
    names: Iterable[str],
    github: str,
    secrets: set[str],
    sandbox: list[str],
) -> tuple[dict[str, str], Found, dict[str, str]]:
    """Probe the configured identities among names under the command's own OS sandbox, since
    what is verified must be what runs; return the command environment too."""
    found: Found = {}
    reasons: dict[str, str] = {}
    token = ""
    if "github" in allow and "github" in names:
        config = str(home / "github" / f"{github}.toml")
        minted, reason = _probe("the token command", [*TOKEN_COMMAND, config], None, _text, secrets)
        if minted is None:
            found["github"], reasons["github"] = None, reason
        else:
            token = minted
            secrets.add(token)
    env = tool_env(home, os.environ, token)
    for name in names:
        if name in allow and name not in found:
            argv = PROBES[name]
            found[name], reason = _probe(argv[0], [*sandbox, *argv], env, PARSERS[name], secrets)
            if reason:
                reasons[name] = reason
    return env, found, reasons


def _load_allow(home: Path) -> dict:
    if not home.is_dir():
        raise Refused(f"no credential home at {home}")
    path = home / "allow.toml"
    try:
        allow = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise Refused(f"no allow list at {path}") from None
    except (OSError, tomllib.TOMLDecodeError) as unreadable:
        raise Refused(f"cannot read {path}: {unreadable}") from None
    if not all(isinstance(allow.get(name, {}), dict) for name in IDENTITIES):
        raise Refused(f"every identity in {path} must be a table")
    return allow


def _stdin_file(home: Path, relative: str) -> Path:
    # Resolving first means a `..` or a symlink cannot lead the command's input out of the home.
    path = (home / relative).resolve()
    if not path.is_relative_to(home.resolve()):
        raise Refused(f"--stdin-file {relative} is outside the credential home")
    if not path.is_file():
        raise Refused(f"--stdin-file {relative} is not a file in the credential home")
    return path


def _run(
    github: str, command: list[str], cwd: Path | None = None, stdin_file: str | None = None
) -> NoReturn:
    if _inside_tools(Path.cwd()):
        raise Refused("the current directory " + OUTSIDE_TOOLS.format(root=tools_root()))
    if cwd is not None and _inside_tools(cwd):
        raise Refused(f"--cwd {cwd} " + OUTSIDE_TOOLS.format(root=tools_root()))
    home = credential_home()
    allow = _load_allow(home)
    if cwd is not None and not cwd.is_dir():
        raise Refused(f"--cwd {cwd} is not a directory")
    source = _stdin_file(home, stdin_file) if stdin_file is not None else None
    secrets = secret_values(home)
    private = _private_tmp(home)
    sandbox = _os_sandbox(private)
    env, found, reasons = _examine(home, allow, IDENTITIES, github, secrets, sandbox)
    problems = violations(allow, found, github)
    if problems:
        lines = [*problems, *(f"{name}: {reason}" for name, reason in reasons.items())]
        raise Refused(redact("refused:\n  " + "\n  ".join(lines), secrets))
    env["TMPDIR"] = str(private)
    command = [*sandbox, *command]
    try:
        if source is not None:
            descriptor = os.open(source, os.O_RDONLY)
            # With descriptor 0 closed on entry the file opens as 0, which closing would undo;
            # and os.open makes it close-on-exec, which only dup2 otherwise clears.
            if descriptor == 0:
                os.set_inheritable(0, True)
            else:
                os.dup2(descriptor, 0)
                os.close(descriptor)
        if cwd is not None:
            os.chdir(cwd)
        os.execvpe(command[0], command, env)
    except OSError as missing:
        raise Refused(f"{command[0]} could not start ({missing.strerror})") from None


def _tools_line(sandbox: list[str]) -> str:
    root = tools_root()
    if _inside_tools(Path.cwd()):
        return "tools FAIL: running from the directory being worked in"
    base = Path(INTERPRETER[0]).resolve()
    for place in (Path.cwd(), real_home() / SANDBOX_PROJECTS):
        if base.is_relative_to(place.resolve()):
            return f"tools FAIL: its interpreter {base} is inside {place}, which the agent writes"
    commit = "unknown"
    try:
        done = subprocess.run(
            [*sandbox, "git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            env=plain_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
        if done.returncode == 0 and done.stdout.strip():
            commit = done.stdout.strip()
    except OSError, subprocess.TimeoutExpired:
        pass
    return f"tools ok: {root} at {commit}"


def _doctor() -> int:
    tools = _tools_line(_os_sandbox())
    print(tools, flush=True)
    home = credential_home()
    allow = _load_allow(home)
    secrets = secret_values(home)
    sandbox = _os_sandbox()
    checks = [(name, "sandbox", name) for name in ("aws", "scaleway", "snowflake")]
    checks += [("github", mode, f"github-{mode}") for mode in GITHUB_MODES]
    failed = not tools.startswith("tools ok: ")
    for name, github, title in checks:
        if name not in allow:
            line = "FAIL: not configured in allow.toml"
        else:
            _, found, reasons = _examine(home, allow, [name], github, secrets, sandbox)
            problems = violations({name: allow[name]}, found, github)
            line = "ok"
            if problems:
                reason = reasons.get(name) or problems[0].partition(": ")[2]
                line = f"FAIL: {redact(reason, secrets)}"
        failed |= line != "ok"
        print(f"{title} {line}")
    line = "absent (not macOS)"
    if sandbox:
        started, reason = _probe(
            "sandbox-exec", [*sandbox, "/usr/bin/true"], None, lambda out: "ok", secrets
        )
        line = "ok" if started else f"FAIL: {reason}"
        failed |= not started
    print(f"os-sandbox {line}")
    return 1 if failed else 0


def _one_line(path: Path) -> str:
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if len(lines) != 1:
        raise Refused(f"{path.name} must hold exactly one line")
    return lines[0]


def _toml_key(path: Path, table: str | None, key: str) -> str:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        raise Refused(f"{path.name} is not valid TOML") from None
    section = data.get(table, {}) if table else data
    value = section.get(key) if isinstance(section, dict) else None
    if not isinstance(value, str) or not value.strip():
        where = f"[{table}] {key}" if table else key
        raise Refused(f"{path.name} has no {where}")
    return value.strip()


def _hostname(path: Path, combination: str) -> str:
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == combination:
            return parts[1]
    raise Refused(f"{path.name} has no line for {combination}")


# Each value `show` prints comes from a file NOT_SECRET names; nothing else is ever printed.
SHOWN: dict[str, tuple[tuple[str, ...], Callable[..., str]]] = {
    "sandbox-org": (("allow.toml",), lambda p: _toml_key(p, "github", "sandbox_org")),
    "upstream-repo": (("allow.toml",), lambda p: _toml_key(p, "github", "upstream_repo")),
    "hostname": (("google", "hostnames.txt"), _hostname),
    "oidc-client-id": (("google", "oidc-client-id"), _one_line),
    "admin-email": (("google", "admin.toml"), lambda p: _toml_key(p, None, "email")),
    "admin-firstname": (("google", "admin.toml"), lambda p: _toml_key(p, None, "firstname")),
}


def _show(name: str, combination: str | None) -> int:
    if name not in SHOWN:
        raise Refused(f"show knows no {name!r}; it shows {', '.join(SHOWN)}")
    if (name == "hostname") != (combination is not None):
        raise Refused("show hostname takes a combination; no other name takes one")
    relative, read = SHOWN[name]
    path = credential_home().joinpath(*relative)
    if not path.is_file():
        raise Refused(f"no {'/'.join(relative)} in the credential home")
    try:
        value = read(path, combination) if combination is not None else read(path)
    except OSError, UnicodeDecodeError:
        raise Refused(f"cannot read {'/'.join(relative)}") from None
    # A secret pasted into the wrong file must not reach the transcript.
    if any(secret in value for secret in secret_values(credential_home())):
        raise Refused(f"{'/'.join(relative)} holds a credential value; it is not shown")
    print(value)
    return 0


class NotListed(Refused):
    pass


def _tracked_text(path: Path, root: Path) -> str | None:
    """What git would publish for one tracked path: a symlink's target, or a file's content."""
    if path.is_symlink():
        return os.readlink(path)
    # A tracked path reached through a symlinked directory leads outside the work tree.
    if not path.resolve().is_relative_to(root) or not path.is_file():
        return None
    return path.read_bytes().decode(errors="ignore")


def _git(directory: Path, sandbox: list[str], env: dict, *args: str, given: bytes = b"") -> bytes:
    try:
        done = subprocess.run(
            [*sandbox, *GIT, *args], cwd=directory, env=env, input=given, capture_output=True
        )
    except OSError as error:
        raise NotListed(f"git could not start in {directory}: {error.strerror}") from None
    if done.returncode != 0:
        lines = done.stderr.decode(errors="replace").strip().splitlines()
        last = lines[-1] if lines else f"exit {done.returncode}"
        raise NotListed(f"cannot read what {directory} would publish: git {args[0]} failed: {last}")
    return done.stdout


def _has_head(directory: Path, sandbox: list[str], env: dict) -> bool:
    """Whether HEAD names a commit; a repository with none yet answers exit 1 and nothing else."""
    command = [*sandbox, *GIT, "rev-parse", "--verify", "--quiet", "HEAD"]
    try:
        done = subprocess.run(command, cwd=directory, env=env, capture_output=True)
    except OSError as error:
        raise NotListed(f"git could not start in {directory}: {error.strerror}") from None
    if done.returncode == 1 and not done.stdout.strip() and not done.stderr.strip():
        return False
    if done.returncode != 0:
        raise NotListed(f"cannot read what {directory} would publish: git rev-parse failed")
    return True


def _names(output: bytes) -> list[str]:
    return [name for name in output.decode(errors="surrogateescape").split("\0") if name]


def _blobs(directory: Path, sandbox: list[str], env: dict, names: list[str]) -> dict[str, str]:
    """The content of each object named, as text, from one `git cat-file --batch`."""
    if not names:
        return {}
    out = _git(
        directory,
        sandbox,
        env,
        "cat-file",
        "--batch",
        given="".join(f"{name}\n" for name in names).encode(),
    )
    contents, position = {}, 0
    while position < len(out):
        header_end = out.index(b"\n", position)
        fields = out[position:header_end].decode(errors="replace").split()
        if len(fields) != 3:
            raise NotListed(
                f"cannot read what {directory} would publish: object {fields[0]} is missing"
            )
        size = int(fields[2])
        contents[fields[0]] = out[header_end + 1 : header_end + 1 + size].decode(errors="ignore")
        position = header_end + 1 + size + 1
    return contents


def _history(directory: Path, sandbox: list[str], env: dict) -> dict[str, set[str]]:
    """Every blob in any commit a ref or HEAD reaches, with every path it had there.

    Each commit is compared with each of its parents, and a root commit with nothing: a blob
    at a path in a commit's tree is either new there against some parent, or the same in a
    parent, whose own comparisons are listed too, so walking back finds where it came in.
    """
    revisions = ["--all", *(["HEAD"] if _has_head(directory, sandbox, env) else [])]
    commits = _git(directory, sandbox, env, "rev-list", *revisions)
    raw = _git(
        directory,
        sandbox,
        env,
        "diff-tree",
        "--stdin",
        "--root",
        "-r",
        "-m",
        "--no-renames",
        "--no-commit-id",
        "--raw",
        "-z",
        given=commits,
    )
    history: dict[str, set[str]] = {}
    fields = raw.decode(errors="surrogateescape").split("\0")
    # Each entry is ":<old mode> <new mode> <old blob> <new blob> <status>" then its path.
    for meta, name in zip(fields[0::2], fields[1::2], strict=False):
        parts = meta.split()
        if not meta.startswith(":") or len(parts) != 5:
            raise NotListed(
                f"cannot read what {directory} would publish: git diff-tree said {meta!r}"
            )
        new_mode, new_blob = parts[1], parts[3]
        # A deleted path has no new blob, and a submodule names another repository's commit.
        if set(new_blob) != {"0"} and new_mode != "160000":
            history.setdefault(new_blob, set()).add(name)
    return history


def secret_hits(directory: Path) -> list[dict]:
    """Every path a push of `directory` could publish holding a value of the credential home,
    with where it was found: the working tree, the index or any commit a ref or HEAD reaches.

    The wrapper reads the secrets and searches the bytes itself; git, which obeys the
    repository's own configuration, lists and reads under the OS sandbox and without a
    credential. A git command that fails refuses the check rather than pass it.
    """
    home = credential_home()
    if not home.is_dir():
        raise Refused(f"no credential home at {home}")
    secrets = secret_values(home)
    sandbox, env = _os_sandbox(), plain_env()
    # Listing from a subdirectory would cover only its part of the work tree and index.
    top = _git(directory, sandbox, env, "rev-parse", "--show-toplevel").decode().strip()
    if Path(top).resolve() != directory.resolve():
        raise Refused(f"{directory} is not the top level of its work tree, {Path(top).resolve()}")
    found: dict[str, set[str]] = {}

    def look(path: str, text: str | None, place: str) -> None:
        if text is not None and any(secret in text for secret in secrets):
            found.setdefault(path, set()).add(place)

    root = directory.resolve()
    staging = _git(
        directory, sandbox, env, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    for name in _names(staging):
        try:
            look(name, _tracked_text(directory / name, root), "working-tree")
        except OSError:
            continue
    staged: dict[str, list[str]] = {}
    for entry in _names(_git(directory, sandbox, env, "ls-files", "-z", "--stage")):
        meta, _, name = entry.partition("\t")
        mode, sha = meta.split()[:2]
        # A submodule entry names a commit of another repository, which this push does not send.
        if mode != "160000":
            staged.setdefault(sha, []).append(name)
    history = _history(directory, sandbox, env)
    blobs = _blobs(directory, sandbox, env, list(dict.fromkeys([*staged, *history])))
    for place, objects in (("staged", staged), ("commit", history)):
        for sha, names in objects.items():
            for name in names:
                look(name, blobs[sha], place)
    return [
        {"path": path, "found": [k for k in PLACES if k in found[path]]} for path in sorted(found)
    ]


def describe(path: str, places: list[str]) -> str:
    """One line for a path that must not be published; never the value that was found."""
    note = f": {REWRITE}" if "commit" in places else ""
    return f"{path}  ({', '.join(PLACES[place] for place in places)}{note})"


def _secrets_check(directory: Path, as_json: bool) -> int:
    try:
        hits = secret_hits(directory)
    except NotListed as refused:
        print(f"access: {refused}", file=sys.stderr)
        return 2
    lines = [json.dumps(hits)] if as_json else [describe(h["path"], h["found"]) for h in hits]
    for line in lines:
        print(line)
    return 1 if hits else 0


def _sibling_github(name: str, args: list[str]) -> str | None:
    """The GitHub App of a sibling call that acts in the test accounts; None for the others."""
    # `cut` is candidate's only subcommand, and a dry run outside the guard would run the
    # checkout's own code, so every candidate call is credentialed.
    if name == "candidate":
        return "upstream"
    if name == "cost" and args[:1] not in (["estimate"], ["spent"]):
        return "sandbox"
    return None


class Usage(Refused):
    pass


def _sibling(name: str, args: list[str]) -> NoReturn:
    # Any other spelling would reach the script's own secret check, which reads a credential
    # home its environment names and wrapped commands can write.
    if (
        name == "project"
        and "check-public" in args
        and not (len(args) == 2 and args[0] == "check-public")
    ):
        raise Usage("give the public check as exactly `project check-public <project>`")
    command = [sys.executable, "-I", str(Path(__file__).with_name(SIBLINGS[name])), *args]
    github = _sibling_github(name, args)
    if github is not None:
        _run(github, command)
    env = plain_env()
    if name == "project" and args[:1] == ["check-public"] and len(args) == 2:
        env[SECRET_HITS] = json.dumps(secret_hits(Path(args[1])))
    command = [*_os_sandbox(), *command]
    try:
        os.execve(command[0], command, env)
    except OSError as missing:
        raise Refused(f"{command[0]} could not start ({missing.strerror})") from None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in SIBLINGS:
        try:
            _sibling(argv[0], argv[1:])
        except Refused as refused:
            print(f"access: {refused}", file=sys.stderr)
            return 2 if isinstance(refused, Usage) else 3
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=f"{', '.join(SIBLINGS)} run the script of that name beside this one with the"
        " arguments that follow, under the OS sandbox; `candidate` and every `cost` call but"
        " `estimate` and `spent` pass the guard first and get the test credentials.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run", help="run one command with the test credentials")
    run.add_argument("--github", choices=GITHUB_MODES, default="sandbox")
    run.add_argument("--cwd", type=Path, help="the directory the command runs in")
    run.add_argument(
        "--stdin-file", help="standard input: a file of the credential home, relative to it"
    )
    run.add_argument("command", nargs=argparse.REMAINDER)
    actions.add_parser("doctor", help="check every identity")
    check = actions.add_parser(
        "secrets-check", help="list the files a push could publish holding a secret"
    )
    check.add_argument("directory", type=Path)
    check.add_argument("--json", action="store_true", help="print the findings as JSON")
    show = actions.add_parser("show", help="print one non-secret setting of the credential home")
    show.add_argument("name", help=", ".join(SHOWN))
    show.add_argument("combination", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            return _doctor()
        if args.action == "secrets-check":
            return _secrets_check(args.directory, args.json)
        if args.action == "show":
            return _show(args.name, args.combination)
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        # The list bounds nothing while `uv run` can start any program; the identity guard, the
        # command's own HOME and environment and the OS sandbox are the bounds. A path is refused
        # so that the list at least names what runs.
        if command and os.sep in command[0]:
            print(f"access: {command[0]} is a path; give a bare executable name", file=sys.stderr)
            return 2
        if not command or command[0] not in ALLOWED_EXECUTABLES:
            allowed = ", ".join(sorted(ALLOWED_EXECUTABLES))
            given = command[0] if command else "no command"
            parser.error(f"{given} is not allowed; the command must start with one of: {allowed}")
        _run(args.github, command, args.cwd, args.stdin_file)
    except Refused as refused:
        print(f"access: {refused}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
