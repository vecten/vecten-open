#!/usr/bin/env python3
"""Run live-check commands with the test credentials, once every tool proves its identity."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
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
TOKEN_COMMAND = [sys.executable, str(Path(__file__).with_name("github_app.py")), "token"]
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
# What the wrapped tools write in the credential home; the rest of it, `allow.toml` above all,
# is the guard's own and stays unwritable.
LIVE_WRITABLE = ("home", "cache", "snowflake/logs", "github/gh")
# Files that steer the agent or every later shell: under the real home, then under the
# directory the wrapper started in.
HOME_UNWRITABLE = (
    ".claude/settings.json", ".claude/settings.local.json", ".zshrc", ".zprofile", ".zshenv",
    ".bashrc", ".bash_profile", ".profile",
)  # fmt: skip
STARTED_IN_UNWRITABLE = (".claude/settings.json", ".claude/settings.local.json")
# Any unix socket may belong to an agent or daemon that acts with the maintainer's access
# (Docker, ssh-agent, gpg-agent), so every one is denied and only the resolver's is open.
ALLOWED_SOCKETS = ("/private/var/run/mDNSResponder",)
# The commands' own HOME holds tool caches and binaries, not the credentials the home was given.
NOT_SECRET = (
    ("allow.toml",), ("google", "hostnames.txt"), ("google", "oidc-client-id"),
    ("snowflake", "logs"), ("github", "gh"), ("git", "config"), ("home",),
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


def real_home() -> Path:
    """The account's own home directory; HOME can name any directory."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def tool_env(home: Path, base: Mapping[str, str], github_token: str) -> dict[str, str]:
    # Any variable a tool reads that the wrapper did not set is a way around the credential
    # home, so only identity-free variables pass, and HOME moves so default lookups miss.
    env = {key: value for key, value in base.items() if key in PASSED or key.startswith("LC_")}
    own_home = home / "home"
    own_home.mkdir(mode=0o700, exist_ok=True)
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
        "AWS_CONFIG_FILE": str(home / "aws/config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(home / "aws/credentials"),
        "AWS_PROFILE": LIVECHECK,
        "SCW_CONFIG_PATH": str(home / "scw/config.yaml"),
        "SNOWFLAKE_HOME": str(home / "snowflake"),
        "SNOWFLAKE_DEFAULT_CONNECTION_NAME": LIVECHECK,
        "GH_CONFIG_DIR": str(home / "github/gh"),
        # Without a token gh answers from the macOS keyring, whatever GH_CONFIG_DIR says.
        "GH_TOKEN": github_token or GH_UNCONFIGURED,
        "VECTEN_LIVE": "1",
        "DOCKER_CONFIG": str(home / "docker"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(git_global) if git_global.is_file() else os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        # A remote left on SSH would authenticate with the maintainer's key.
        "GIT_SSH_COMMAND": SSH_REFUSED,
        "UV_CACHE_DIR": str(real_home() / ".cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(real_home() / ".local/share/uv/python"),
        "GIT_CONFIG_COUNT": str(len(git)),
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


def sandbox_profile(account_home: Path, live_home: Path, started_in: Path) -> str:
    """A Seatbelt profile that keeps tools, and slips, off the maintainer's credentials.

    It allows everything by default and denies what it names, so it does not contain a process
    that sets out to get out: such a process can still write most of the home directory, read
    any credential file the profile does not name, and start processes outside the profile
    through LaunchServices, Apple Events or launchd.
    """
    env_files = " ".join(f'(regex #"{env_files_pattern(v)}")' for v in _variants(account_home))
    credentials = _filters((kind, account_home / relative) for kind, relative in DENIED)
    unwritable = _filters(
        [
            *(("literal", account_home / relative) for relative in HOME_UNWRITABLE),
            *(("literal", started_in / relative) for relative in STARTED_IN_UNWRITABLE),
        ]
    )
    # The later rule wins: the projects' allow reopens their `.env` files but no credential, the
    # writable parts of the credential home follow its deny, and no allow follows the deny of
    # the files that steer the agent. The resolver's socket follows the deny of every socket.
    sockets = "".join(f'(allow network-outbound (literal "{path}"))' for path in ALLOWED_SOCKETS)
    return (
        "(version 1)(allow default)"
        f"(deny file-read* {env_files})"
        f"(allow file-read* {_filters([('subpath', account_home / SANDBOX_PROJECTS)])})"
        f"(deny file-read* file-write* {credentials})"
        f"(deny file-write* {_filters([('subpath', live_home)])})"
        f"(allow file-write* {_filters(('subpath', live_home / p) for p in LIVE_WRITABLE)})"
        f"(deny file-write* {unwritable})"
        f"(deny network-outbound (remote unix-socket)){sockets}"
    )


def _os_sandbox() -> list[str]:
    """The argv prefix that runs a command under the deny profile; empty off macOS."""
    if PLATFORM != "darwin":
        return []
    return [SANDBOX_EXEC, "-p", sandbox_profile(real_home(), credential_home(), Path.cwd())]


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


def secret_values(home: Path) -> set[str]:
    values = set()
    for directory, subdirectories, names in os.walk(home):
        here = Path(directory)
        subdirectories[:] = [name for name in subdirectories if not _skipped(home, here / name)]
        for path in (here / name for name in names):
            if not _skipped(home, path) and path.is_file():
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
    try:
        text = path.read_text(errors="ignore")
    except OSError:
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
    home = credential_home()
    allow = _load_allow(home)
    if cwd is not None and not cwd.is_dir():
        raise Refused(f"--cwd {cwd} is not a directory")
    source = _stdin_file(home, stdin_file) if stdin_file is not None else None
    secrets = secret_values(home)
    sandbox = _os_sandbox()
    env, found, reasons = _examine(home, allow, IDENTITIES, github, secrets, sandbox)
    problems = violations(allow, found, github)
    if problems:
        lines = [*problems, *(f"{name}: {reason}" for name, reason in reasons.items())]
        raise Refused(redact("refused:\n  " + "\n  ".join(lines), secrets))
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


def _doctor() -> int:
    home = credential_home()
    allow = _load_allow(home)
    secrets = secret_values(home)
    sandbox = _os_sandbox()
    checks = [(name, "sandbox", name) for name in ("aws", "scaleway", "snowflake")]
    checks += [("github", mode, f"github-{mode}") for mode in GITHUB_MODES]
    failed = False
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


def _secrets_check(directory: Path) -> int:
    home = credential_home()
    if not home.is_dir():
        raise Refused(f"no credential home at {home}")
    secrets = secret_values(home)
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=directory, capture_output=True)
    if listed.returncode != 0:
        print(f"access: {directory} is not a git work tree", file=sys.stderr)
        return 2
    hits = []
    for name in filter(None, listed.stdout.decode().split("\0")):
        path = directory / name
        if path.is_file() and any(s in path.read_bytes().decode(errors="ignore") for s in secrets):
            hits.append(name)
    for name in hits:
        print(name)
    return 1 if hits else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run", help="run one command with the test credentials")
    run.add_argument("--github", choices=GITHUB_MODES, default="sandbox")
    run.add_argument("--cwd", type=Path, help="the directory the command runs in")
    run.add_argument(
        "--stdin-file", help="standard input: a file of the credential home, relative to it"
    )
    run.add_argument("command", nargs=argparse.REMAINDER)
    actions.add_parser("doctor", help="check every identity")
    check = actions.add_parser("secrets-check", help="list tracked files holding a secret")
    check.add_argument("directory", type=Path)
    show = actions.add_parser("show", help="print one non-secret setting of the credential home")
    show.add_argument("name", help=", ".join(SHOWN))
    show.add_argument("combination", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.action == "doctor":
            return _doctor()
        if args.action == "secrets-check":
            return _secrets_check(args.directory)
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
