"""A `gh` executable for the skill evaluations: GitHub on the local filesystem.

It answers the calls `vecten github`, `vecten bootstrap`, `vecten deploy` and `vecten ship` make
(read from their source, `ANSWERED` lists them) and the pull-request calls the `operate` and
`semantic` skills make. Repositories are bare git repositories under the state directory;
`install` points `https://github.com/` at them through git's `insteadOf`, so remotes read as on
GitHub. A dispatched `terraform-apply.yml` stores the mocked cloud's outputs and ships every
host; `deploy-platform.yml`, and a push to the default branch of a deployed project, ship as
`ship.yml` does. Every call is appended to `calls.jsonl` for the grader; secret values are
never written anywhere.

Run as `python -m eval_harness.gh_standin <gh arguments>`; `install` writes the `gh` wrapper.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

STATE_VAR = "EVAL_HARNESS_GH_DIR"
USER_VAR = "EVAL_HARNESS_GH_USER"
DEFAULT_USER = "acme-dev"
GITHUB = "https://github.com/"
# Owns every evaluation repository; git takes the longest `insteadOf`, so this outranks a
# person's rewrite of all of github.com.
OWNER = "acme"
VERSION = "gh version 2.83.0 (eval harness gh stand-in)"
FIXTURES = Path(__file__).resolve().parents[1]
MOCK_CLOUD_DIR = FIXTURES / "mock_cloud"
APPLY_WORKFLOW = "terraform-apply.yml"
SHIP_WORKFLOW = "deploy-platform.yml"
PUSH_WORKFLOW = "ship.yml"
# The git variables a hook inherits from the push that ran it; a clone inside the hook must
# not see them, or it would act on the bare repository instead of its own checkout.
HOOK_GIT_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_QUARANTINE_PATH",
    "GIT_PREFIX",
    "GIT_PUSH_OPTION_COUNT",
)

ANSWERED = {
    ("--version", None),
    ("version", None),
    ("auth", "status"),
    ("repo", "create"),
    ("repo", "view"),
    ("api", None),
    ("variable", "set"),
    ("variable", "list"),
    ("secret", "set"),
    ("secret", "list"),
    ("workflow", "run"),
    ("workflow", "list"),
    ("run", "list"),
    ("run", "watch"),
    ("run", "view"),
    ("pr", "create"),
    ("pr", "list"),
    ("pr", "view"),
    ("pr", "edit"),
    ("pr", "comment"),
    ("pr", "review"),
    ("pr", "merge"),
    ("pr", "close"),
    ("pr", "checks"),
}


class Unanswered(Exception):
    """A call the stand-in has no answer for; logged so the grader sees it."""


class GhError(Exception):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.message, self.status, self.body = message, status, body


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def install(directory: Path, *, python: str, environ: dict[str, str] | None = None) -> dict:
    """Write the `gh` wrapper under `directory`; the variables a shell needs to use it."""
    directory = Path(directory).resolve()
    (directory / "bin").mkdir(parents=True, exist_ok=True)
    (directory / "repos").mkdir(exist_ok=True)
    wrapper = directory / "bin" / "gh"
    pythonpath = f"{FIXTURES}{os.pathsep}{MOCK_CLOUD_DIR}"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export {STATE_VAR}={shlex.quote(str(directory))}\n"
        f'PYTHONPATH={shlex.quote(pythonpath)}"${{PYTHONPATH:+:$PYTHONPATH}}" '
        f'exec {shlex.quote(python)} -m eval_harness.gh_standin "$@"\n'
    )
    wrapper.chmod(0o755)
    base = environ if environ is not None else os.environ
    return {
        "PATH": f"{directory / 'bin'}{os.pathsep}{base.get('PATH', '')}",
        STATE_VAR: str(directory),
        **git_rewrite(directory, base),
    }


def git_rewrite(directory: Path, environ: dict[str, str]) -> dict[str, str]:
    """git configuration from the environment that sends `https://github.com/` to the bare
    repositories, appended after any such configuration the environment already holds."""
    count = int(environ.get("GIT_CONFIG_COUNT") or 0)
    return {
        "GIT_CONFIG_COUNT": str(count + 1),
        f"GIT_CONFIG_KEY_{count}": f"url.{Path(directory) / 'repos' / OWNER}/.insteadOf",
        f"GIT_CONFIG_VALUE_{count}": f"{GITHUB}{OWNER}/",
    }


def mock_cloud_environ(cloud_dir: Path, environ: dict[str, str]) -> dict[str, str]:
    """What selects the mocked cloud in a `vecten` process, as `scripted-human.md` lists it."""
    existing = environ.get("PYTHONPATH")
    return {
        "PYTHONPATH": os.pathsep.join(filter(None, [str(MOCK_CLOUD_DIR), existing])),
        "VECTEN_CONTROL_PLANE": "vecten_mock_cloud:MockCloud",
        "VECTEN_MOCK_CLOUD_DIR": str(Path(cloud_dir).resolve()),
    }


def _hook_text(directory: Path) -> str:
    pythonpath = f"{FIXTURES}{os.pathsep}{MOCK_CLOUD_DIR}"
    return (
        "#!/bin/sh\n"
        f"export {STATE_VAR}={shlex.quote(str(directory))}\n"
        f'PYTHONPATH={shlex.quote(pythonpath)}"${{PYTHONPATH:+:$PYTHONPATH}}" '
        f"exec {shlex.quote(sys.executable)} -m eval_harness.gh_standin --post-receive\n"
    )


def _numeric_id(text: str) -> int:
    return 100_000 + zlib.crc32(text.encode()) % 900_000_000


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def bare(self, name: str) -> Path:
        return self.root / "repos" / f"{name}.git"

    def _meta(self, name: str) -> Path:
        return self.root / "repos" / f"{name}.json"

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self, name: str) -> dict | None:
        path = self._meta(name)
        return json.loads(path.read_text()) if path.is_file() else None

    def require(self, name: str) -> dict:
        repo = self.read(name)
        if repo is None:
            raise GhError(
                f"GraphQL: Could not resolve to a Repository with the name '{name}'. (repository)",
                status=404,
            )
        return repo

    @contextmanager
    def edit(self, name: str):
        with self._locked():
            repo = self.require(name)
            yield repo
            self._meta(name).write_text(json.dumps(repo, indent=2) + "\n")

    def create(self, name: str, private: bool) -> dict:
        with self._locked():
            if self.read(name) is not None:
                raise GhError(
                    "GraphQL: Name already exists on this account (createRepository)", status=422
                )
            bare = self.bare(name)
            bare.parent.mkdir(parents=True, exist_ok=True)
            _run(["git", "init", "-q", "--bare", "-b", "main", str(bare)])
            hook = bare / "hooks" / "post-receive"
            hook.write_text(_hook_text(self.root))
            hook.chmod(0o755)
            alias = bare.with_suffix("")
            if not alias.exists():
                alias.symlink_to(bare.name)
            owner = name.split("/")[0]
            repo = {
                "full_name": name,
                "id": _numeric_id(name),
                "owner": {"login": owner, "id": _numeric_id(owner)},
                "private": private,
                "default_branch": "main",
                "environments": {},
                "variables": {"repository": {}, "environments": {}},
                "secrets": {"repository": {}, "environments": {}},
                "pulls": {},
                "runs": [],
            }
            self._meta(name).write_text(json.dumps(repo, indent=2) + "\n")
            return repo

    def names(self) -> list[str]:
        base = self.root / "repos"
        return sorted(str(path.relative_to(base).with_suffix("")) for path in base.glob("*/*.json"))

    def log(self, entry: dict) -> None:
        with self._locked():
            with (self.root / "calls.jsonl").open("a") as handle:
                handle.write(json.dumps(entry) + "\n")


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in HOOK_GIT_VARS}


def _run(args: list[str], cwd: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, env=_clean_env(), **kwargs
    )
    if result.returncode != 0:
        raise GhError(f"{' '.join(args[:3])} failed: {result.stderr.strip()[-500:]}")
    return result


def _git_head(bare: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    return result.stdout.strip() or None


def options(
    args: list[str], valued: dict[str, str], flags: dict[str, str] | None = None
) -> tuple[list[str], dict[str, Any]]:
    """Split `args` into positionals and options; `valued` and `flags` map spellings to keys.

    A valued option given twice keeps every value, in order. An unknown flag is unanswered,
    since a guess at its meaning could answer differently from GitHub.
    """
    flags = flags or {}
    positional: list[str] = []
    found: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, inline = arg.partition("=")
        if arg.startswith("--") and eq and name in valued:
            found.setdefault(valued[name], []).append(inline)
            i += 1
        elif arg in valued:
            if i + 1 >= len(args):
                raise GhError(f"flag needs an argument: {arg}")
            found.setdefault(valued[arg], []).append(args[i + 1])
            i += 2
        elif arg in flags:
            found[flags[arg]] = True
            i += 1
        elif arg.startswith("-") and arg != "-":
            raise Unanswered(f"unknown flag {arg}")
        else:
            positional.append(arg)
            i += 1
    return positional, found


def last(found: dict[str, Any], key: str, default: Any = None) -> Any:
    values = found.get(key)
    return values[-1] if values else default


JSON_OPTS = {"--json": "json", "--jq": "jq", "-q": "jq", "--template": "template", "-t": "template"}


def emit(value: Any, found: dict[str, Any]) -> str:
    if last(found, "template"):
        raise Unanswered("--template")
    text = json.dumps(value, indent=2)
    expression = last(found, "jq")
    if expression is None:
        return text + "\n"
    if shutil.which("jq") is None:
        raise Unanswered("--jq needs the jq executable on the PATH")
    result = subprocess.run(["jq", "-r", expression], input=text, capture_output=True, text=True)
    if result.returncode != 0:
        raise GhError(result.stderr.strip())
    return result.stdout


def select(value: dict, fields: str) -> dict:
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    unknown = [f for f in wanted if f not in value]
    if unknown:
        raise GhError(
            f"Unknown JSON field: {unknown[0]!r}\nAvailable fields:\n  "
            + "\n  ".join(sorted(value))
        )
    return {f: value[f] for f in wanted}


class Context:
    def __init__(self, store: Store, cwd: Path, user: str, stdin_text):
        self.store, self.cwd, self.user = store, cwd, user
        self._stdin = stdin_text
        self.stdin_used = False
        self.value_in_argv = False

    def stdin(self) -> str:
        self.stdin_used = True
        return self._stdin()

    def repository(self, found: dict[str, Any]) -> str:
        """The repository named by --repo, else the one the checkout's origin points at."""
        named = last(found, "repo")
        if named:
            return named.removeprefix(GITHUB).removesuffix(".git")
        # `git remote get-url` applies insteadOf; the configured URL is the GitHub spelling.
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        url = result.stdout.strip()
        if result.returncode != 0 or not url.startswith(GITHUB):
            raise GhError("no git remotes found; pass --repo owner/name")
        return url.removeprefix(GITHUB).removesuffix(".git")

    def branch(self) -> str:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise GhError("could not determine the current branch")
        return result.stdout.strip()


# Commands. Each returns the text for stdout and the exit code.


def cmd_version(ctx: Context, args: list[str]) -> tuple[str, int]:
    return VERSION + "\n", 0


def cmd_auth(ctx: Context, args: list[str]) -> tuple[str, int]:
    if args[:1] != ["status"]:
        raise Unanswered("auth " + " ".join(args[:1]))
    options(args[1:], {"--hostname": "host", "-h": "host"}, {"--show-token": "t", "-t": "t"})
    return (
        f"github.com\n  ✓ Logged in to github.com account {ctx.user} (eval harness gh "
        "stand-in)\n  - Active account: true\n"
    ), 0


def repo_api(repo: dict) -> dict:
    name = repo["full_name"]
    return {
        "id": repo["id"],
        "name": name.split("/")[1],
        "full_name": name,
        "owner": {**repo["owner"], "type": "Organization"},
        "private": repo["private"],
        "visibility": "private" if repo["private"] else "public",
        "default_branch": repo["default_branch"],
        "html_url": GITHUB + name,
        "clone_url": f"{GITHUB}{name}.git",
    }


def repo_graphql(repo: dict) -> dict:
    name = repo["full_name"]
    return {
        "id": f"R_{repo['id']}",
        "name": name.split("/")[1],
        "nameWithOwner": name,
        "owner": {"login": repo["owner"]["login"]},
        "defaultBranchRef": {"name": repo["default_branch"]},
        "url": GITHUB + name,
        "isPrivate": repo["private"],
        "visibility": "PRIVATE" if repo["private"] else "PUBLIC",
        "description": "",
    }


def cmd_repo(ctx: Context, args: list[str]) -> tuple[str, int]:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    if sub == "create":
        positional, found = options(
            rest,
            {
                "--source": "source",
                "-s": "source",
                "--remote": "remote",
                "-r": "remote",
                "--description": "description",
                "-d": "description",
                "--homepage": "homepage",
            },
            {
                "--private": "private",
                "--public": "public",
                "--internal": "internal",
                "--push": "push",
                "--confirm": "confirm",
                "-y": "confirm",
                "--disable-issues": "x",
                "--disable-wiki": "x",
            },
        )
        if len(positional) != 1:
            raise Unanswered("repo create without exactly one owner/name")
        name = positional[0]
        if "/" not in name:
            name = f"{ctx.user}/{name}"
        ctx.store.create(name, private=not found.get("public"))
        source = last(found, "source")
        if source:
            where = (ctx.cwd / source).resolve()
            remote = last(found, "remote", "origin")
            _run(["git", "remote", "add", remote, f"{GITHUB}{name}.git"], cwd=where)
            if found.get("push"):
                _run(["git", "push", "-q", "--set-upstream", remote, "HEAD"], cwd=where)
        return f"{GITHUB}{name}\n", 0
    if sub == "view":
        positional, found = options(rest, {"--repo": "repo", "-R": "repo", **JSON_OPTS})
        if any(a in ("--web", "-w") for a in rest):
            raise Unanswered("repo view --web")
        name = positional[0] if positional else ctx.repository(found)
        repo = repo_graphql(ctx.store.require(name))
        if last(found, "json"):
            return emit(select(repo, last(found, "json")), found), 0
        return f"name:\t{repo['nameWithOwner']}\ndescription:\t\n--\n", 0
    raise Unanswered(f"repo {sub}")


def typed(value: str) -> Any:
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def put_field(body: dict, key: str, value: Any) -> None:
    parts = re.findall(r"[^\[\]]+", key)
    target = body
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def not_found(path: str) -> GhError:
    body = {
        "message": "Not Found",
        "documentation_url": "https://docs.github.com/rest",
        "status": "404",
    }
    return GhError("gh: Not Found (HTTP 404)", status=404, body=body)


def pull_api(repo: dict, pull: dict) -> dict:
    name = repo["full_name"]
    merged = pull["state"] == "merged"
    return {
        "number": pull["number"],
        "title": pull["title"],
        "body": pull["body"],
        "state": "open" if pull["state"] == "open" else "closed",
        "merged": merged,
        "merged_at": pull.get("merged_at"),
        "merge_commit_sha": pull.get("merge_commit_sha"),
        "draft": pull.get("draft", False),
        "user": {"login": pull["author"]},
        "head": {"ref": pull["head"], "sha": pull["head_sha"], "repo": {"full_name": name}},
        "base": {"ref": pull["base"], "repo": {"full_name": name}},
        "html_url": f"{GITHUB}{name}/pull/{pull['number']}",
        "created_at": pull["created_at"],
    }


def review_api(review: dict) -> dict:
    return {
        "id": review["id"],
        "user": {"login": review["author"]},
        "state": review["state"],
        "body": review.get("body", ""),
        "commit_id": review["commit_id"],
        "submitted_at": review["submitted_at"],
    }


def search_issues(ctx: Context, query: str) -> dict:
    """GitHub's issue search over the stand-in's pull requests, for the qualifiers shipping
    and the skills use: repo:, is:pr, is:open, is:closed, is:merged, in:body and free text."""
    repo_name = None
    states: set[str] = set()
    words = shlex.split(query)
    text = []
    for word in words:
        if word.startswith("repo:"):
            repo_name = word[5:]
        elif word in ("is:pr", "type:pr", "in:body", "in:title", "is:pull-request"):
            continue
        elif word.startswith("is:"):
            states.add(word[3:])
        else:
            text.append(word)
    items = []
    for name in [repo_name] if repo_name else ctx.store.names():
        repo = ctx.store.read(name) if name else None
        if repo is None:
            continue
        for pull in repo["pulls"].values():
            state = pull["state"]
            if "merged" in states and state != "merged":
                continue
            if "open" in states and state != "open":
                continue
            if "closed" in states and state == "open":
                continue
            haystack = f"{pull['title']}\n{pull['body']}"
            if all(t in haystack for t in text):
                items.append({**pull_api(repo, pull), "pull_request": {}})
    items.sort(key=lambda item: item["number"], reverse=True)
    return {"total_count": len(items), "incomplete_results": False, "items": items}


def cmd_api(ctx: Context, args: list[str]) -> tuple[str, int]:
    positional, found = options(
        args,
        {
            "-X": "method",
            "--method": "method",
            "-F": "field",
            "--field": "field",
            "-f": "raw",
            "--raw-field": "raw",
            "-H": "header",
            "--header": "header",
            "--input": "input",
            "--jq": "jq",
            "-q": "jq",
            "--hostname": "host",
            "--cache": "cache",
        },
        {"--paginate": "paginate", "--slurp": "slurp", "--silent": "silent"},
    )
    if len(positional) != 1:
        raise Unanswered("api without exactly one endpoint")
    body: dict = {}
    for item in found.get("field", []):
        key, _, value = item.partition("=")
        put_field(body, key, typed(value))
    for item in found.get("raw", []):
        key, _, value = item.partition("=")
        put_field(body, key, value)
    if last(found, "input"):
        source = last(found, "input")
        body.update(json.loads(ctx.stdin() if source == "-" else Path(source).read_text()))
    method = (last(found, "method") or ("POST" if body else "GET")).upper()
    endpoint = positional[0]
    if "{owner}" in endpoint or "{repo}" in endpoint:
        # gh fills these placeholders from the checkout's repository.
        owner, repo_name = ctx.repository({}).split("/", 1)
        endpoint = endpoint.replace("{owner}", owner).replace("{repo}", repo_name)
    parsed = urlparse(endpoint.lstrip("/"))
    path, query = parsed.path, parse_qs(parsed.query)
    answer = route_api(ctx, method, path, query, body)
    if found.get("paginate") and found.get("slurp"):
        answer = [answer]
    if found.get("silent"):
        return "", 0
    return emit(answer, found), 0


def route_api(ctx: Context, method: str, path: str, query: dict, body: dict) -> Any:
    if path == "user" and method == "GET":
        return {"login": ctx.user, "id": _numeric_id(ctx.user), "type": "User"}
    if path == "search/issues" and method == "GET":
        return search_issues(ctx, query.get("q", [""])[0])
    match = re.fullmatch(r"repos/([^/]+/[^/]+)(?:/(.*))?", path)
    if not match:
        raise Unanswered(f"api {method} {path}")
    name, rest = match[1], match[2] or ""
    if ctx.store.read(name) is None:
        raise not_found(path)
    if rest == "" and method == "GET":
        return repo_api(ctx.store.require(name))
    env_match = re.fullmatch(r"environments/([^/]+)(/deployment-branch-policies)?", rest)
    if env_match:
        env, policies = env_match[1], env_match[2]
        with ctx.store.edit(name) as repo:
            environment = repo["environments"].get(env)
            if policies:
                if environment is None:
                    raise not_found(path)
                if method == "POST":
                    policy = {
                        "id": len(environment["branch_policies"]) + 1,
                        "name": body.get("name"),
                        "type": body.get("type", "branch"),
                    }
                    environment["branch_policies"].append(policy)
                    return policy
                if method == "GET":
                    listed = environment["branch_policies"]
                    return {"total_count": len(listed), "branch_policies": listed}
            elif method == "GET":
                if environment is None:
                    raise not_found(path)
                return {"name": env, **environment}
            elif method == "PUT":
                environment = environment or {
                    "deployment_branch_policy": None,
                    "branch_policies": [],
                    "created_at": now_iso(),
                }
                if "deployment_branch_policy" in body:
                    environment["deployment_branch_policy"] = body["deployment_branch_policy"]
                repo["environments"][env] = environment
                return {"name": env, **environment}
        raise Unanswered(f"api {method} {path}")
    status_match = re.fullmatch(r"statuses/([0-9a-f]{40})|commits/([0-9a-f]{7,40})/status", rest)
    if status_match and method == "POST" and status_match[1]:
        status = {
            "state": body.get("state"),
            "context": body.get("context", "default"),
            "description": body.get("description"),
            "target_url": body.get("target_url"),
            "created_at": now_iso(),
        }
        with ctx.store.edit(name) as repo:
            repo.setdefault("statuses", {}).setdefault(status_match[1], []).append(status)
        return status
    if status_match and method == "GET" and status_match[2]:
        found = ctx.store.require(name).get("statuses", {})
        sha = next((s for s in found if s.startswith(status_match[2])), status_match[2])
        # The combined status holds the latest status of each context.
        latest = {s["context"]: s for s in found.get(sha, [])}
        return {"sha": sha, "state": "pending", "statuses": list(latest.values())}
    repo = ctx.store.require(name)
    if rest == "pulls" and method == "GET":
        state = query.get("state", ["open"])[0]
        head = query.get("head", [None])[0]
        pulls = [
            pull_api(repo, p)
            for p in repo["pulls"].values()
            if (state == "all" or (p["state"] == "open") == (state == "open"))
            and (head is None or head.split(":")[-1] == p["head"])
        ]
        return sorted(pulls, key=lambda p: p["number"], reverse=True)
    pull_match = re.fullmatch(r"pulls/(\d+)(/reviews)?", rest)
    if pull_match and method == "GET":
        pull = repo["pulls"].get(pull_match[1])
        if pull is None:
            raise not_found(path)
        if pull_match[2]:
            return [review_api(r) for r in pull.get("reviews", [])]
        return pull_api(repo, pull)
    raise Unanswered(f"api {method} {path}")


def _scope(found: dict[str, Any]) -> tuple[str, str | None]:
    env = last(found, "env")
    return ("environments", env) if env else ("repository", None)


def cmd_variable(ctx: Context, args: list[str]) -> tuple[str, int]:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    valued = {"--env": "env", "-e": "env", "--repo": "repo", "-R": "repo", **JSON_OPTS}
    if sub == "set":
        positional, found = options(rest, {**valued, "--body": "body", "-b": "body"})
        if len(positional) != 1:
            raise Unanswered("variable set without one name")
        value = last(found, "body")
        if value is None:
            value = ctx.stdin().rstrip("\n")
        name = ctx.repository(found)
        kind, env = _scope(found)
        with ctx.store.edit(name) as repo:
            target = repo["variables"][kind]
            if env:
                target = target.setdefault(env, {})
            target[positional[0]] = {"value": value, "updated_at": now_iso()}
        return "", 0
    if sub == "list":
        _, found = options(rest, valued)
        repo = ctx.store.require(ctx.repository(found))
        kind, env = _scope(found)
        values = repo["variables"][kind]
        values = values.get(env, {}) if env else values
        rows = [
            {"name": k, "value": v["value"], "updatedAt": v["updated_at"]}
            for k, v in sorted(values.items())
        ]
        if last(found, "json"):
            return emit([select(r, last(found, "json")) for r in rows], found), 0
        return "".join(f"{r['name']}\t{r['value']}\t{r['updatedAt']}\n" for r in rows), 0
    raise Unanswered(f"variable {sub}")


def cmd_secret(ctx: Context, args: list[str]) -> tuple[str, int]:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    valued = {"--env": "env", "-e": "env", "--repo": "repo", "-R": "repo", "--app": "app"}
    if sub == "set":
        positional, found = options(rest, {**valued, "--body": "body", "-b": "body"})
        if len(positional) != 1:
            raise Unanswered("secret set without one name")
        if last(found, "body") is not None:
            ctx.value_in_argv = True
        elif not ctx.stdin():
            raise GhError("no value on standard input for the secret")
        name = ctx.repository(found)
        kind, env = _scope(found)
        with ctx.store.edit(name) as repo:
            target = repo["secrets"][kind]
            if env:
                target = target.setdefault(env, {})
            target[positional[0]] = {"updated_at": now_iso()}
        return "", 0
    if sub == "list":
        _, found = options(rest, {**valued, **JSON_OPTS})
        repo = ctx.store.require(ctx.repository(found))
        kind, env = _scope(found)
        values = repo["secrets"][kind]
        values = values.get(env, {}) if env else values
        rows = [{"name": k, "updatedAt": v["updated_at"]} for k, v in sorted(values.items())]
        if last(found, "json"):
            return emit([select(r, last(found, "json")) for r in rows], found), 0
        return "".join(f"{r['name']}\tUpdated {r['updatedAt']}\n" for r in rows), 0
    raise Unanswered(f"secret {sub}")


# Workflows.


def run_fields(repo: dict, run: dict) -> dict:
    return {
        "databaseId": run["id"],
        "number": run["id"] - 1000,
        "workflowName": run["workflow"].removesuffix(".yml"),
        "name": run["workflow"].removesuffix(".yml"),
        "displayTitle": run["title"],
        "event": run["event"],
        "headBranch": run["branch"],
        "headSha": run["head_sha"],
        "createdAt": run["created_at"],
        "updatedAt": run["created_at"],
        "startedAt": run["created_at"],
        "status": "completed",
        "conclusion": run["conclusion"],
        "url": f"{GITHUB}{repo['full_name']}/actions/runs/{run['id']}",
    }


def ci_checkout(store: Store, name: str, ref: str) -> tuple[Path, str]:
    """A fresh clone of the repository at `ref`, as actions/checkout makes; (path, commit)."""
    workdir = Path(tempfile.mkdtemp(prefix="gh-standin-ci-"))
    checkout = workdir / "repo"
    _run(["git", "clone", "-q", str(store.bare(name)), str(checkout)])
    _run(["git", "checkout", "-q", ref], cwd=checkout)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
    return checkout, commit


def deployed_outputs(config, env: str) -> dict:
    """The mocked cloud's outputs for this project: its hosts, under its domain."""
    from vecten_mock_cloud import MockCloud

    outputs = json.loads(json.dumps(MockCloud.OUTPUTS))
    domain = config.deploy.domain
    hosts = outputs["hosts"]
    hosts["platform"]["hostnames"] = [f"airflow.{domain}"]
    hosts["mcp"]["hostnames"] = [f"mcp.{domain}"]
    for component in ("platform", "mcp"):
        if not config.has(component):
            hosts.pop(component)
    outputs["setup_host"] = "mcp" if "mcp" in hosts else "platform"
    if config.warehouse.existing:
        outputs["postgres"].pop("warehouse")
    outputs["environment"] = env
    outputs["secret_prefix"] = f"vecten/{config.project.name}/{env}/"
    return outputs


def emulate_apply(checkout: Path, env: str, commit: str) -> str:
    """What `vecten deploy` does inside the apply job, against the mocked cloud."""
    from vecten_mock_cloud import MockCloud

    from vecten.deploy.guards import require_bootstrapped
    from vecten_shared.config import load_config

    config = load_config(checkout)
    require_bootstrapped(config, env)
    plane = MockCloud(checkout, env)
    outputs = plane.store_outputs(deployed_outputs(config, env))
    hosts = plane.ship_hosts(checkout, config, env, outputs, commit)
    return f"applied the mocked cloud for {env}; shipped {commit} to {', '.join(hosts)}"


def emulate_ship(store: Store, checkout: Path, env: str) -> str:
    """The ship job: `vecten ship --env <env>` inside Actions, from the checkout.

    The job sets its commit status through `gh`, which must be this stand-in: the real one
    would write to GitHub.
    """
    environ = _clean_env()
    result = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", "ship", "--env", env, "--json"],
        cwd=checkout,
        capture_output=True,
        text=True,
        env={
            **environ,
            **install(store.root, python=sys.executable, environ=environ),
            "GITHUB_ACTIONS": "true",
        },
    )
    if result.returncode != 0:
        raise RuntimeError(f"vecten ship exited {result.returncode}: {result.stdout}")
    return result.stdout.strip()


def record_run(
    store: Store, name: str, workflow: str, event: str, branch: str, ref: str, emulate
) -> dict:
    """Run `emulate(checkout, commit)` on a checkout of `ref` and record the run."""
    created = now_iso()
    lines = [f"{created} {workflow} ({event}) on {branch}"]
    commit = _git_head(store.bare(name), ref) or ""
    conclusion = "success"
    try:
        checkout, commit = ci_checkout(store, name, ref)
        try:
            outcome = emulate(checkout, commit)
            if outcome:
                lines.append(outcome)
        finally:
            shutil.rmtree(checkout.parent, ignore_errors=True)
    except Exception as exc:
        conclusion = "failure"
        message = getattr(exc, "message", None) or str(exc)
        lines += [f"error: {message}", traceback.format_exc()]
    with store.edit(name) as repo:
        run = {
            "id": 1000 + len(repo["runs"]) + 1,
            "workflow": workflow,
            "title": workflow.removesuffix(".yml"),
            "event": event,
            "branch": branch,
            "head_sha": commit,
            "created_at": created,
            "conclusion": conclusion,
        }
        logs = store.root / "runs"
        logs.mkdir(exist_ok=True)
        (logs / f"{run['id']}.log").write_text("\n".join(lines) + "\n")
        repo["runs"].append(run)
    return run


def dispatch(store: Store, name: str, workflow: str, ref: str, inputs: dict) -> dict:
    env = inputs.get("environment", "prod")
    if workflow == APPLY_WORKFLOW:

        def emulate(checkout, commit):
            return emulate_apply(checkout, env, commit)

    elif workflow == SHIP_WORKFLOW:

        def emulate(checkout, commit):
            return emulate_ship(store, checkout, env)

    else:

        def emulate(checkout, commit):
            return f"the stand-in does not emulate {workflow}; the run did nothing"

    return record_run(store, name, workflow, "workflow_dispatch", ref, ref, emulate)


def find_workflow(store: Store, name: str, ref: str, workflow: str) -> str:
    bare = store.bare(name)
    result = subprocess.run(
        ["git", "--git-dir", str(bare), "ls-tree", "--name-only", ref, ".github/workflows/"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    files = [Path(line).name for line in result.stdout.split()]
    for candidate in (workflow, f"{workflow}.yml", f"{workflow}.yaml"):
        if candidate in files:
            return candidate
    raise GhError(f"could not find any workflows named {workflow}", status=404)


def cmd_workflow(ctx: Context, args: list[str]) -> tuple[str, int]:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    if sub == "run":
        positional, found = options(
            rest,
            {
                "--ref": "ref",
                "-r": "ref",
                "-f": "raw",
                "--raw-field": "raw",
                "-F": "field",
                "--field": "field",
                "--repo": "repo",
                "-R": "repo",
            },
            {"--json": "stdin_json"},
        )
        if len(positional) != 1:
            raise Unanswered("workflow run without one workflow")
        name = ctx.repository(found)
        repo = ctx.store.require(name)
        ref = last(found, "ref") or repo["default_branch"]
        workflow = find_workflow(ctx.store, name, ref, positional[0])
        inputs = dict(item.partition("=")[::2] for item in found.get("raw", []))
        inputs.update(dict(item.partition("=")[::2] for item in found.get("field", [])))
        dispatch(ctx.store, name, workflow, ref, inputs)
        sys.stderr.write(f"✓ Created workflow_dispatch event for {workflow} at {ref}\n")
        return "", 0
    if sub == "list":
        _, found = options(rest, {"--repo": "repo", "-R": "repo", **JSON_OPTS})
        name = ctx.repository(found)
        repo = ctx.store.require(name)
        bare = ctx.store.bare(name)
        listed = subprocess.run(
            [
                "git",
                "--git-dir",
                str(bare),
                "ls-tree",
                "--name-only",
                repo["default_branch"],
                ".github/workflows/",
            ],
            capture_output=True,
            text=True,
            env=_clean_env(),
        ).stdout.split()
        return "".join(f"{Path(p).stem}\tactive\t{Path(p).name}\n" for p in listed), 0
    raise Unanswered(f"workflow {sub}")


def _run_by_id(ctx: Context, found: dict, run_id: str) -> tuple[dict, dict]:
    repo = ctx.store.require(ctx.repository(found))
    for run in repo["runs"]:
        if str(run["id"]) == run_id:
            return repo, run
    raise GhError(f"could not find any workflow run with ID {run_id}", status=404)


def cmd_run(ctx: Context, args: list[str]) -> tuple[str, int]:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    repo_opts = {"--repo": "repo", "-R": "repo"}
    if sub == "list":
        _, found = options(
            rest,
            {
                **repo_opts,
                **JSON_OPTS,
                "--workflow": "workflow",
                "-w": "workflow",
                "--event": "event",
                "-e": "event",
                "--branch": "branch",
                "-b": "branch",
                "--limit": "limit",
                "-L": "limit",
                "--status": "status",
                "-s": "status",
                "--commit": "commit",
                "-c": "commit",
            },
        )
        repo = ctx.store.require(ctx.repository(found))
        workflow = last(found, "workflow")
        runs = [
            run
            for run in repo["runs"]
            if (workflow is None or workflow in (run["workflow"], run["title"]))
            and (last(found, "event") in (None, run["event"]))
            and (last(found, "branch") in (None, run["branch"]))
            and (last(found, "commit") in (None, run["head_sha"]))
        ]
        runs = sorted(runs, key=lambda r: r["id"], reverse=True)[: int(last(found, "limit", 20))]
        rows = [run_fields(repo, run) for run in runs]
        if last(found, "json"):
            return emit([select(r, last(found, "json")) for r in rows], found), 0
        return "".join(
            f"completed\t{r['conclusion']}\t{r['displayTitle']}\t{r['workflowName']}\t"
            f"{r['headBranch']}\t{r['event']}\t{r['databaseId']}\n"
            for r in rows
        ), 0
    if sub == "watch":
        positional, found = options(
            rest,
            {**repo_opts, "--interval": "interval", "-i": "interval"},
            {"--exit-status": "exit_status", "--compact": "compact"},
        )
        if len(positional) != 1:
            raise Unanswered("run watch without a run id")
        repo, run = _run_by_id(ctx, found, positional[0])
        text = f"✓ {run['workflow']} run {run['id']} completed with '{run['conclusion']}'\n"
        failed = run["conclusion"] != "success"
        return text, 1 if failed and found.get("exit_status") else 0
    if sub == "view":
        positional, found = options(
            rest,
            {**repo_opts, **JSON_OPTS, "--job": "job", "-j": "job"},
            {
                "--log": "log",
                "--log-failed": "log",
                "--verbose": "verbose",
                "-v": "verbose",
                "--exit-status": "exit_status",
            },
        )
        if any(a in ("--web", "-w") for a in rest) or len(positional) != 1:
            raise Unanswered("run view without one run id")
        repo, run = _run_by_id(ctx, found, positional[0])
        if found.get("log"):
            return (ctx.store.root / "runs" / f"{run['id']}.log").read_text(), 0
        fields = run_fields(repo, run)
        if last(found, "json"):
            return emit(select(fields, last(found, "json")), found), 0
        code = 1 if found.get("exit_status") and run["conclusion"] != "success" else 0
        return (
            f"{run['conclusion']} {run['branch']} {fields['workflowName']} · {run['id']}\n"
            f"Triggered via {run['event']}\n\nFor the log: gh run view {run['id']} --log\n"
        ), code
    raise Unanswered(f"run {sub}")


# Pull requests.


def pr_fields(repo: dict, pull: dict) -> dict:
    reviews = pull.get("reviews", [])
    approved = any(r["state"] == "APPROVED" for r in reviews)
    return {
        "number": pull["number"],
        "title": pull["title"],
        "body": pull["body"],
        "state": pull["state"].upper(),
        "url": f"{GITHUB}{repo['full_name']}/pull/{pull['number']}",
        "headRefName": pull["head"],
        "headRefOid": pull["head_sha"],
        "baseRefName": pull["base"],
        "author": {"login": pull["author"]},
        "isDraft": pull.get("draft", False),
        "closed": pull["state"] != "open",
        "createdAt": pull["created_at"],
        "mergedAt": pull.get("merged_at"),
        "mergeCommit": {"oid": pull["merge_commit_sha"]} if pull.get("merge_commit_sha") else None,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED" if approved else "REVIEW_REQUIRED",
        "reviews": [
            {
                "author": {"login": r["author"]},
                "state": r["state"],
                "body": r.get("body", ""),
                "submittedAt": r["submitted_at"],
                "commit": {"oid": r["commit_id"]},
            }
            for r in reviews
        ],
        "comments": [
            {"author": {"login": c["author"]}, "body": c["body"], "createdAt": c["created_at"]}
            for c in pull.get("comments", [])
        ],
        "labels": [{"name": label} for label in pull.get("labels", [])],
    }


def _find_pull(ctx: Context, repo: dict, selector: str | None) -> dict:
    if selector is None:
        selector = ctx.branch()
    selector = selector.rsplit("/pull/", 1)[-1] if selector.startswith(GITHUB) else selector
    selector = selector.removeprefix("#")
    if selector.isdigit() and selector in repo["pulls"]:
        return repo["pulls"][selector]
    matches = [p for p in repo["pulls"].values() if p["head"] == selector]
    open_first = sorted(matches, key=lambda p: (p["state"] != "open", -p["number"]))
    if not open_first:
        raise GhError(f"no pull requests found for {selector!r}")
    return open_first[0]


def _body(ctx: Context, found: dict) -> str | None:
    if last(found, "body_file") is not None:
        source = last(found, "body_file")
        return ctx.stdin() if source == "-" else (ctx.cwd / source).read_text()
    return last(found, "body")


PR_BODY_OPTS = {"--body": "body", "-b": "body", "--body-file": "body_file", "-F": "body_file"}


def cmd_pr(ctx: Context, args: list[str]) -> tuple[str, int]:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    repo_opts = {"--repo": "repo", "-R": "repo"}
    if sub == "create":
        _, found = options(
            rest,
            {
                **repo_opts,
                **PR_BODY_OPTS,
                "--title": "title",
                "-t": "title",
                "--base": "base",
                "-B": "base",
                "--head": "head",
                "-H": "head",
                "--label": "label",
                "-l": "label",
                "--assignee": "assignee",
                "-a": "assignee",
                "--reviewer": "reviewer",
                "-r": "reviewer",
                "--milestone": "milestone",
                "-m": "milestone",
            },
            {"--draft": "draft", "-d": "draft", "--fill": "fill", "-f": "fill"},
        )
        name = ctx.repository(found)
        repo = ctx.store.require(name)
        head = last(found, "head") or ctx.branch()
        head = head.split(":")[-1]
        base = last(found, "base") or repo["default_branch"]
        head_sha = _git_head(ctx.store.bare(name), f"refs/heads/{head}")
        if head_sha is None:
            raise GhError(
                "aborted: you must first push the current branch to a remote, or use the "
                "--head flag"
            )
        title, body = last(found, "title"), _body(ctx, found)
        if found.get("fill") and title is None:
            log = _run(["git", "log", "-1", "--format=%s%n%n%b", head_sha], cwd=ctx.cwd).stdout
            title, _, fill_body = log.partition("\n\n")
            body = body if body is not None else fill_body.strip()
        if not title:
            raise GhError(
                "must provide `--title` and `--body` (or `--fill`) when not running interactively"
            )
        with ctx.store.edit(name) as repo:
            for pull in repo["pulls"].values():
                if pull["head"] == head and pull["state"] == "open":
                    raise GhError(
                        f'a pull request for branch "{head}" into branch "{base}" already '
                        f"exists:\n{GITHUB}{name}/pull/{pull['number']}"
                    )
            number = len(repo["pulls"]) + 1
            repo["pulls"][str(number)] = {
                "number": number,
                "title": title,
                "body": body or "",
                "head": head,
                "head_sha": head_sha,
                "base": base,
                "author": ctx.user,
                "draft": bool(found.get("draft")),
                "state": "open",
                "created_at": now_iso(),
                "labels": found.get("label", []),
                "reviews": [],
                "comments": [],
            }
        return f"{GITHUB}{name}/pull/{number}\n", 0
    if sub == "list":
        _, found = options(
            rest,
            {
                **repo_opts,
                **JSON_OPTS,
                "--state": "state",
                "-s": "state",
                "--head": "head",
                "-H": "head",
                "--base": "base",
                "-B": "base",
                "--search": "search",
                "-S": "search",
                "--limit": "limit",
                "-L": "limit",
                "--author": "author",
                "-A": "author",
                "--label": "label",
                "-l": "label",
            },
            {"--draft": "draft", "-d": "draft"},
        )
        name = ctx.repository(found)
        repo = ctx.store.require(name)
        state = last(found, "state", "open")
        pulls = list(repo["pulls"].values())
        if last(found, "search"):
            hits = {i["number"] for i in search_issues(ctx, last(found, "search"))["items"]}
            pulls = [p for p in pulls if p["number"] in hits]
        pulls = [
            p
            for p in pulls
            if (
                state == "all"
                or p["state"] == state
                or (state == "closed" and p["state"] == "merged")
            )
            and last(found, "head") in (None, p["head"])
            and last(found, "base") in (None, p["base"])
            and last(found, "author") in (None, p["author"])
        ]
        pulls = sorted(pulls, key=lambda p: -p["number"])[: int(last(found, "limit", 30))]
        rows = [pr_fields(repo, p) for p in pulls]
        if last(found, "json"):
            return emit([select(r, last(found, "json")) for r in rows], found), 0
        return "".join(
            f"{r['number']}\t{r['title']}\t{r['headRefName']}\t{r['state']}\n" for r in rows
        ), 0
    if sub == "view":
        positional, found = options(
            rest, {**repo_opts, **JSON_OPTS}, {"--comments": "c", "-c": "c"}
        )
        if any(a in ("--web", "-w") for a in rest):
            raise Unanswered("pr view --web")
        repo = ctx.store.require(ctx.repository(found))
        fields = pr_fields(repo, _find_pull(ctx, repo, positional[0] if positional else None))
        if last(found, "json"):
            return emit(select(fields, last(found, "json")), found), 0
        return (
            f"{fields['title']} #{fields['number']}\n{fields['state']} • "
            f"{fields['author']['login']} wants to merge into {fields['baseRefName']} from "
            f"{fields['headRefName']}\n\n{fields['body']}\n\nView this pull request on GitHub: "
            f"{fields['url']}\n"
        ), 0
    if sub in ("edit", "comment", "close", "review"):
        valued = {**repo_opts, **PR_BODY_OPTS}
        flags: dict[str, str] = {}
        if sub == "edit":
            valued |= {
                "--title": "title",
                "-t": "title",
                "--add-label": "label",
                "--remove-label": "unlabel",
            }
        elif sub == "review":
            flags = {
                "--approve": "approve",
                "-a": "approve",
                "--request-changes": "changes",
                "-r": "changes",
                "--comment": "commented",
                "-c": "commented",
            }
        elif sub == "close":
            valued |= {"--comment": "body", "-c": "body"}
            flags = {"--delete-branch": "delete", "-d": "delete"}
        positional, found = options(rest, valued, flags)
        name = ctx.repository(found)
        with ctx.store.edit(name) as repo:
            pull = _find_pull(ctx, repo, positional[0] if positional else None)
            body = _body(ctx, found)
            if sub == "edit":
                if last(found, "title") is not None:
                    pull["title"] = last(found, "title")
                if body is not None:
                    pull["body"] = body
                pull["labels"] = sorted(
                    set(pull.get("labels", []))
                    | set(found.get("label", [])) - set(found.get("unlabel", []))
                )
            elif sub == "comment" or (
                sub == "review" and not (found.get("approve") or found.get("changes"))
            ):
                text = body
                if text is None:
                    raise GhError("a comment needs --body or --body-file")
                pull.setdefault("comments", []).append(
                    {"author": ctx.user, "body": text, "created_at": now_iso()}
                )
            elif sub == "review":
                pull.setdefault("reviews", []).append(
                    {
                        "id": sum(len(p.get("reviews", [])) for p in repo["pulls"].values()) + 1,
                        "author": ctx.user,
                        "state": "APPROVED" if found.get("approve") else "CHANGES_REQUESTED",
                        "body": body or "",
                        "commit_id": pull["head_sha"],
                        "submitted_at": now_iso(),
                    }
                )
            elif sub == "close":
                if pull["state"] == "open":
                    pull["state"] = "closed"
            number = pull["number"]
        return f"{GITHUB}{name}/pull/{number}\n", 0
    if sub == "merge":
        positional, found = options(
            rest,
            {
                **repo_opts,
                **PR_BODY_OPTS,
                "--subject": "subject",
                "-t": "subject",
                "--match-head-commit": "match",
            },
            {
                "--merge": "merge",
                "-m": "merge",
                "--squash": "squash",
                "-s": "squash",
                "--rebase": "rebase",
                "-r": "rebase",
                "--delete-branch": "delete",
                "-d": "delete",
                "--admin": "admin",
                "--auto": "auto",
            },
        )
        name = ctx.repository(found)
        repo = ctx.store.require(name)
        pull = _find_pull(ctx, repo, positional[0] if positional else None)
        if pull["state"] != "open":
            raise GhError(f"Pull request #{pull['number']} is not open")
        merge_commit = merge_pull(ctx.store, name, pull, squash=bool(found.get("squash")))
        with ctx.store.edit(name) as repo:
            stored = repo["pulls"][str(pull["number"])]
            stored.update(
                state="merged",
                merged_at=now_iso(),
                merge_commit_sha=merge_commit,
                merged_by=ctx.user,
            )
        sys.stderr.write(f"✓ Merged pull request #{pull['number']} ({pull['title']})\n")
        return "", 0
    if sub == "checks":
        positional, found = options(
            rest, {**repo_opts, **JSON_OPTS}, {"--watch": "w", "--required": "r"}
        )
        repo = ctx.store.require(ctx.repository(found))
        pull = _find_pull(ctx, repo, positional[0] if positional else None)
        sys.stderr.write(f"no checks reported on the '{pull['head']}' branch\n")
        return "", 1
    raise Unanswered(f"pr {sub}")


def merge_pull(store: Store, name: str, pull: dict, *, squash: bool) -> str:
    """Merge the pull request's head into its base on the bare repository, as GitHub's merge
    button does; the push runs the repository's hook, so a deployed project ships."""
    workdir = Path(tempfile.mkdtemp(prefix="gh-standin-merge-"))
    try:
        checkout = workdir / "repo"
        _run(["git", "clone", "-q", str(store.bare(name)), str(checkout)])
        identity = ["-c", "user.name=GitHub", "-c", "user.email=noreply@github.com"]
        _run(["git", "checkout", "-q", pull["base"]], cwd=checkout)
        message = f"Merge pull request #{pull['number']} from {name.split('/')[0]}/{pull['head']}"
        if squash:
            _run(["git", "merge", "-q", "--squash", f"origin/{pull['head']}"], cwd=checkout)
            _run(
                ["git", *identity, "commit", "-q", "-m", f"{pull['title']} (#{pull['number']})"],
                cwd=checkout,
            )
        else:
            _run(
                [
                    "git",
                    *identity,
                    "merge",
                    "-q",
                    "--no-ff",
                    "-m",
                    f"{message}\n\n{pull['title']}",
                    f"origin/{pull['head']}",
                ],
                cwd=checkout,
            )
        commit = _run(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip()
        pushed = subprocess.run(
            ["git", "push", "-q", "origin", pull["base"]],
            cwd=checkout,
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        sys.stderr.write(pushed.stderr)
        if pushed.returncode != 0:
            raise GhError(f"merging failed: {pushed.stderr.strip()[-300:]}")
        return commit
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def post_receive(store: Store, bare: Path, lines: list[str]) -> None:
    """`ship.yml` on a push to the default branch: ship when the pushed vecten.toml records a
    deployed commit for prod, as its gate job reads it, else finish green having done nothing."""
    name = str(bare.resolve().relative_to((store.root / "repos").resolve()))
    name = name.removesuffix(".git")
    repo = store.read(name)
    if repo is None:
        return
    for line in lines:
        old, new, ref = (line.split() + ["", "", ""])[:3]
        if ref != f"refs/heads/{repo['default_branch']}" or set(new) == {"0"}:
            continue
        try:
            find_workflow(store, name, new, PUSH_WORKFLOW)
        except GhError:
            continue

        def emulate(checkout, commit):
            from vecten_shared.config import load_config

            facts = load_config(checkout).environments.get("prod")
            if facts is None or not facts.deployed_commit:
                return "gate: prod is not deployed in this commit; nothing shipped"
            return emulate_ship(store, checkout, "prod")

        run = record_run(store, name, PUSH_WORKFLOW, "push", repo["default_branch"], new, emulate)
        sys.stderr.write(f"{PUSH_WORKFLOW} run {run['id']}: {run['conclusion']}\n")


COMMANDS = {
    "--version": cmd_version,
    "version": cmd_version,
    "auth": cmd_auth,
    "repo": cmd_repo,
    "api": cmd_api,
    "variable": cmd_variable,
    "secret": cmd_secret,
    "workflow": cmd_workflow,
    "run": cmd_run,
    "pr": cmd_pr,
}


def _redacted(argv: list[str]) -> list[str]:
    if argv[:2] != ["secret", "set"]:
        return list(argv)
    out, hide = [], False
    for arg in argv:
        if hide:
            out.append("[redacted]")
            hide = False
            continue
        if arg in ("--body", "-b"):
            hide = True
        elif arg.startswith("--body="):
            arg = "--body=[redacted]"
        out.append(arg)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    store = Store(Path(os.environ[STATE_VAR]))
    if argv[:1] == ["--post-receive"]:
        post_receive(store, Path.cwd(), sys.stdin.read().splitlines())
        return 0
    cached: list[str] = []

    def read_stdin() -> str:
        if not cached:
            cached.append("" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read())
        return cached[0]

    ctx = Context(store, Path.cwd(), os.environ.get(USER_VAR) or DEFAULT_USER, read_stdin)
    entry: dict[str, Any] = {"time": now_iso(), "argv": _redacted(argv), "cwd": str(ctx.cwd)}
    command = COMMANDS.get(argv[0]) if argv else None
    try:
        if command is None:
            raise Unanswered(" ".join(argv[:2]))
        text, code = command(ctx, argv[1:])
        sys.stdout.write(text)
        entry["answered"] = True
    except Unanswered as exc:
        sys.stderr.write(
            f"the gh stand-in (vecten eval harness) does not answer `gh {shlex.join(argv)}` "
            f"({exc}); it answers what vecten and the skills need. The call is logged in "
            f"{store.root / 'calls.jsonl'}.\n"
        )
        code, entry["answered"] = 1, False
    except GhError as exc:
        if exc.body is not None:
            sys.stdout.write(json.dumps(exc.body) + "\n")
        sys.stderr.write(exc.message + "\n")
        code, entry["answered"] = 1, True
    entry.update(exit=code, stdin=ctx.stdin_used)
    if ctx.value_in_argv:
        entry["value_in_argv"] = True
    store.log(entry)
    return code


if __name__ == "__main__":
    sys.exit(main())
