"""Rendered AWS projects, environment outputs and fakes for SSM, git and gh."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import ConnectorDecl, load_config, save_config

from .scaffold_matrix import DEPLOY, LOCAL

REGION = "eu-central-1"
# PowerUserAccess exists in every account; moto loads AWS managed policies only on request.
MANAGED_POLICIES = {"iam": {"load_aws_managed_policies": True}}
BUCKET = "vecten-acme-prod-data"
STATE_BUCKET = "vecten-acme-prod-state-123456789012"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
STATE_HOST = "vecten-acme-prod-state.c1.eu-central-1.rds.amazonaws.com"
WAREHOUSE_HOST = "vecten-acme-prod-warehouse.c1.eu-central-1.rds.amazonaws.com"

STANDARD_OUTPUTS: dict = {
    "environment": "prod",
    "region": REGION,
    "hosts": {
        "platform": {
            "instance_id": "i-0platform",
            "public_ip": "203.0.113.10",
            "components": ["platform"],
            "hostnames": ["airflow.data.acme.com"],
        },
        "mcp": {
            "instance_id": "i-0mcp",
            "public_ip": "203.0.113.11",
            "components": ["mcp"],
            "hostnames": ["mcp.data.acme.com"],
        },
    },
    "setup_host": "mcp",
    "bucket": BUCKET,
    "secret_prefix": "vecten/acme/prod/",
    "postgres": {
        "state": {
            "host": STATE_HOST,
            "port": 5432,
            "databases": ["airflow", "mcp_state"],
            "master_secret": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds-state",
        },
        "warehouse": {
            "host": WAREHOUSE_HOST,
            "port": 5432,
            "databases": ["warehouse"],
            "master_secret": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds-wh",
        },
    },
}


def minimal_outputs() -> dict:
    outputs = copy.deepcopy(STANDARD_OUTPUTS)
    outputs["hosts"] = {
        "main": {
            "instance_id": "i-0main",
            "public_ip": "203.0.113.12",
            "components": ["platform", "mcp"],
            "hostnames": ["airflow.data.acme.com", "mcp.data.acme.com"],
        }
    }
    outputs["setup_host"] = "main"
    outputs["postgres"] = {
        "state": {
            "host": STATE_HOST,
            "port": 5432,
            "databases": ["airflow", "mcp_state", "warehouse"],
            "master_secret": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds-state",
        }
    }
    return outputs


def render_aws(root: Path, cloud: str = "aws", **overrides) -> Path:
    answers = {**LOCAL, "sources": ["hubspot"], "warehouse": "postgres", **DEPLOY, "cloud": cloud}
    render_project(root, ScaffoldParams.from_answers(answers, overrides))
    return root


def add_connector(root: Path, name: str = "hubspot", secrets: tuple[str, ...] = ()) -> None:
    config = load_config(root)
    config.connectors.append(
        ConnectorDecl(name=name, source="dlt", secrets=list(secrets), dependencies=[])
    )
    save_config(root, config)
    package = root / "connectors" / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("def source():\n    return []\n")


def write_outputs(root: Path, outputs: dict, env: str = "prod") -> Path:
    path = root / ".vecten" / "environments" / f"{env}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outputs), encoding="utf-8")
    return path


def set_facts(root: Path, env: str = "prod", **facts) -> None:
    config = load_config(root)
    for key, value in facts.items():
        setattr(config.environments[env], key, value)
    save_config(root, config)


def aws_credentials(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Fake credentials, and no shared config, so nothing can reach a real account."""
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("VECTEN_ENVIRONMENT_FILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(root / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(root / "no-aws-credentials"))


def no_aws_credentials(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    for key in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(root / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(root / "no-aws-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


class FakeSsm:
    """Records `send_command`; each invocation answers from `results[(instance, kind)]`.

    `kind` is `ship` or `rollback`, read from the agent subcommand in the commands.
    """

    def __init__(self, results: dict | None = None, pending: int = 0):
        self.results = results or {}
        self.sent: list[dict] = []
        self.pending = pending
        # SendCommand answers InvalidInstanceId this many times before it accepts a command.
        self.unregistered = 0
        self.attempts = 0
        # Kinds (`ship`, `rollback`) whose SendCommand fails with AccessDeniedException.
        self.refused: set[str] = set()
        self._invocations: dict[str, dict] = {}
        self._polls: dict[str, int] = {}

    def send_command(self, **kwargs):
        from botocore.exceptions import ClientError

        self.attempts += 1
        if self.unregistered:
            self.unregistered -= 1
            raise ClientError(
                {
                    "Error": {
                        "Code": "InvalidInstanceId",
                        "Message": "Instances not in a valid state",
                    }
                },
                "SendCommand",
            )
        script = "\n".join(kwargs["Parameters"]["commands"])
        if ("rollback" if "apply.py rollback" in script else "ship") in self.refused:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "LOADER_PASSWORD=x"}},
                "SendCommand",
            )
        self.sent.append(kwargs)
        command_id = f"cmd-{len(self.sent)}"
        instance = kwargs["InstanceIds"][0]
        script = "\n".join(kwargs["Parameters"]["commands"])
        kind = "rollback" if "apply.py rollback" in script else "ship"
        status, stderr = self.results.get((instance, kind), ("Success", ""))
        self._invocations[command_id] = {"Status": status, "StandardErrorContent": stderr}
        return {"Command": {"CommandId": command_id}}

    def get_command_invocation(self, CommandId, InstanceId):
        polls = self._polls.get(CommandId, 0)
        self._polls[CommandId] = polls + 1
        if polls < self.pending:
            return {"Status": "InProgress", "StandardErrorContent": "", "ResponseCode": -1}
        found = self._invocations[CommandId]
        code = 0 if found["Status"] == "Success" else 1
        return {
            "Status": found["Status"],
            "StandardOutputContent": "",
            "StandardErrorContent": found["StandardErrorContent"],
            "ResponseCode": code,
        }

    def kinds(self) -> list[tuple[str, str]]:
        out = []
        for sent in self.sent:
            script = "\n".join(sent["Parameters"]["commands"])
            kind = "rollback" if "apply.py rollback" in script else "ship"
            out.append((sent["InstanceIds"][0], kind))
        return out


class FakeGit:
    """`answers` maps a call's leading words (`("merge-base",)`) to `(exit code, stdout)`;
    every other call succeeds with no output."""

    def __init__(self, head: str = COMMIT, answers: dict | None = None):
        self.head = head
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def which(self):
        return "/usr/bin/git"

    def run(self, args, cwd):
        self.calls.append(list(args))
        if args == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, self.head + "\n", "")
        for prefix, (code, stdout) in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix:
                stderr = "" if code == 0 else f"fatal: git {args[0]} failed"
                return subprocess.CompletedProcess(args, code, stdout, stderr)
        return subprocess.CompletedProcess(args, 0, "", "")

    def named(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


CUSTOM_BRANCH_POLICY = {"protected_branches": False, "custom_branch_policies": True}
# The numeric ids `gh api repos/<repo>` answers, which GitHub's immutable OIDC subjects name.
OWNER_ID = 9001
REPOSITORY_ID = 4242
BRANCH_POLICIES_SUFFIX = "/deployment-branch-policies"


def gh_fields(args: list[str]) -> dict[str, str]:
    """The `-f`/`-F` fields of a `gh api` call."""
    found = {}
    for flag, value in zip(args, args[1:], strict=False):
        if flag in ("-f", "-F"):
            key, _, val = value.partition("=")
            found[key] = val
    return found


class FakeGh:
    """Answers the calls `dispatch_and_follow` makes; `watch_code` is the watched run's exit.

    `history` is the default branch newest first, as the GraphQL history search pages it;
    `statuses` maps a commit to its `(context, state)` statuses oldest first, and every
    `api -X POST .../statuses/<sha>` appends to it.
    """

    def __init__(
        self,
        watch_code: int = 0,
        created_at: str = "2999-01-01T00:00:00Z",
        logged_in: bool = True,
        canonical: str = "acme/data",
        failing: tuple[str, ...] = (),
        environments: tuple[str, ...] = (),
        branch_policies: bool = True,
        branch_policy_status: int | None = None,
        environment_policies: dict[str, tuple[dict | None, list[str]]] | None = None,
        history: tuple[str, ...] | list[str] = (),
        statuses: dict[str, list[tuple[str, str]]] | None = None,
    ):
        self.history = list(history)
        self.statuses: dict[str, list[tuple[str, str]]] = {
            sha: list(found) for sha, found in (statuses or {}).items()
        }
        self.watch_code = watch_code
        self.created_at = created_at
        self.logged_in = logged_in
        # The repository as GitHub spells it, answered by `repo view`.
        self.canonical = canonical
        # Leading words (`api`, `variable`) of calls that exit 1.
        self.failing = failing
        # `repos/<repo>/environments/<env>` paths that exist, each with its
        # deployment_branch_policy and the branch names its custom policies allow; `environments`
        # exist restricted to main, and a PUT adds its path.
        self.environments: dict[str, tuple[dict | None, list[str]]] = {
            path: (dict(CUSTOM_BRANCH_POLICY), ["main"]) for path in environments
        }
        self.environments.update(environment_policies or {})
        # False: a PUT carrying a deployment branch policy answers HTTP 422, as on a private
        # repository of a free plan.
        self.branch_policies = branch_policies
        # The HTTP status a POST of a deployment branch policy fails with, if any.
        self.branch_policy_status = branch_policy_status
        self.calls: list[list[str]] = []
        # (argv, stdin) of every call that wrote to gh's standard input.
        self.inputs: list[tuple[list[str], str]] = []

    def which(self):
        return "/usr/bin/gh"

    def _fail(self, args, status: int, text: str):
        return subprocess.CompletedProcess(args, 1, "", f"gh: {text} (HTTP {status})")

    def latest_status(self, sha: str, context: str) -> str | None:
        states = [state for ctx, state in self.statuses.get(sha, []) if ctx == context]
        return states[-1] if states else None

    def posted_statuses(self) -> list[tuple[str, str, str]]:
        """(commit, context, state) of every status set, in order."""
        return [
            (c[3].rsplit("/", 1)[-1], gh_fields(c)["context"], gh_fields(c)["state"])
            for c in self.calls
            if c[:3] == ["api", "-X", "POST"] and "/statuses/" in c[3]
        ]

    def _graphql(self, args):
        fields = gh_fields(args)
        start = int(fields["cursor"]) if fields.get("cursor") else 0
        size = int(fields["first"])
        nodes = []
        for sha in self.history[start : start + size]:
            state = self.latest_status(sha, fields["context"])
            status = None if state is None else {"context": {"state": state.upper()}}
            nodes.append({"oid": sha, "status": status})
        more = start + size < len(self.history)
        history = {
            "pageInfo": {"hasNextPage": more, "endCursor": str(start + size)},
            "nodes": nodes,
        }
        body = {"data": {"repository": {"ref": {"target": {"history": history}}}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    def _get(self, args):
        path = args[1]
        if path.endswith("/status") and "/commits/" in path:
            sha = path.split("/")[-2]
            latest = {ctx: state for ctx, state in self.statuses.get(sha, [])}
            body = {"sha": sha, "statuses": [{"context": c, "state": s} for c, s in latest.items()]}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if path.lower() == f"repos/{self.canonical}".lower():
            body = {"full_name": self.canonical, "id": REPOSITORY_ID, "owner": {"id": OWNER_ID}}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        env_path = path.removesuffix(BRANCH_POLICIES_SUFFIX)
        if env_path not in self.environments:
            return self._fail(args, 404, "Not Found")
        policy, branches = self.environments[env_path]
        if path.endswith(BRANCH_POLICIES_SUFFIX):
            if not (policy or {}).get("custom_branch_policies"):
                return self._fail(args, 404, "Not Found")
            body = {
                "total_count": len(branches),
                "branch_policies": [{"name": b, "type": "branch"} for b in branches],
            }
        else:
            body = {"name": env_path.rsplit("/", 1)[-1], "deployment_branch_policy": policy}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    def _api(self, args):
        if args[:2] == ["api", "graphql"]:
            return self._graphql(args)
        if "-X" not in args:
            return self._get(args)
        method, path = args[args.index("-X") + 1], args[args.index("-X") + 2]
        if method == "POST" and "/statuses/" in path:
            fields = gh_fields(args)
            sha = path.rsplit("/", 1)[-1]
            self.statuses.setdefault(sha, []).append((fields["context"], fields["state"]))
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if method == "PUT":
            restricting = any("deployment_branch_policy[" in a for a in args)
            if not self.branch_policies and restricting:
                return self._fail(args, 422, "Validation Failed")
            self.environments[path] = (dict(CUSTOM_BRANCH_POLICY) if restricting else None, [])
        if method == "POST" and self.branch_policy_status:
            return self._fail(args, self.branch_policy_status, "Not Found")
        if method == "POST":
            env_path = path.removesuffix(BRANCH_POLICIES_SUFFIX)
            name = next(a.removeprefix("name=") for a in args if a.startswith("name="))
            self.environments[env_path][1].append(name)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    def run(self, args, cwd, input=None):
        self.calls.append(list(args))
        if input is not None:
            self.inputs.append((list(args), input))
        if args[:1] and args[0] in self.failing:
            return self._fail(args, 500, "Server Error")
        if args[:2] == ["auth", "status"]:
            if not self.logged_in:
                return subprocess.CompletedProcess(args, 1, "", "You are not logged into any hosts")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:1] == ["api"]:
            return self._api(args)
        if args[:1] == ["variable"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["secret", "set"]:
            if not input:
                return subprocess.CompletedProcess(args, 1, "", "no value on stdin")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["repo", "view"]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"nameWithOwner": self.canonical, "defaultBranchRef": {"name": "main"}}),
                "",
            )
        if args[:2] == ["workflow", "run"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["run", "list"]:
            runs = [
                {
                    "databaseId": 41,
                    "headSha": "old",
                    "createdAt": "2000-01-01T00:00:00Z",
                    "status": "completed",
                },
                {
                    "databaseId": 42,
                    "headSha": COMMIT,
                    "createdAt": self.created_at,
                    "status": "queued",
                },
            ]
            return subprocess.CompletedProcess(args, 0, json.dumps(runs), "")
        if args[:2] == ["run", "watch"]:
            return subprocess.CompletedProcess(args, self.watch_code, "", "")
        if args[:2] == ["run", "view"]:
            conclusion = "success" if self.watch_code == 0 else "failure"
            body = {
                "conclusion": conclusion,
                "url": "https://github.com/acme/data/actions/runs/42",
                "headSha": COMMIT,
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(args, 1, "", f"unexpected gh call {args}")

    def named(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]
