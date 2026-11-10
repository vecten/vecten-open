"""Projects and environment outputs the provider tests build on."""

from __future__ import annotations

import base64
import copy
import json
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from vecten_shared.config import VectenConfig, save_config

REGION = "eu-west-1"
STATE_HOST = "vecten-acme-prod-state.c1.eu-west-1.rds.amazonaws.com"
WAREHOUSE_HOST = "vecten-acme-prod-warehouse.c1.eu-west-1.rds.amazonaws.com"
BUCKET = "vecten-acme-prod-data"

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
            "master_secret": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:rds-state",
        },
        "warehouse": {
            "host": WAREHOUSE_HOST,
            "port": 5432,
            "databases": ["warehouse"],
            "master_secret": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:rds-wh",
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
            "master_secret": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:rds-state",
        }
    }
    return outputs


def write_config(
    root: Path,
    name: str = "acme",
    warehouse: dict | None = None,
    environments: dict | None = None,
) -> None:
    raw: dict = {"users": 3, "project": {"name": name, "scaffold_version": "0.1.0"}}
    if warehouse is not None:
        raw["warehouse"] = warehouse
    if environments is not None:
        raw["environments"] = environments
    save_config(root, VectenConfig.model_validate(raw))


def write_outputs(root: Path, outputs: dict, env: str = "prod") -> Path:
    path = root / ".vecten" / "environments" / f"{env}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outputs), encoding="utf-8")
    return path


def write_aws_project(
    root: Path, outputs: dict | None = None, warehouse: dict | None = None
) -> None:
    write_config(root, warehouse=warehouse)
    write_outputs(root, STANDARD_OUTPUTS if outputs is None else outputs)


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


SCW_REGION = "fr-par"
SCW_PROJECT_ID = "11111111-2222-3333-4444-555555555555"
SCW_SECRET_KEY = "scw-secret-key-for-tests"
SCW_API = f"https://api.scaleway.com/secret-manager/v1beta1/regions/{SCW_REGION}"


def scaleway_outputs() -> dict:
    outputs = copy.deepcopy(STANDARD_OUTPUTS)
    outputs["region"] = SCW_REGION
    outputs["secret_prefix"] = "/vecten/acme/prod/"
    return outputs


def write_scaleway_project(
    root: Path,
    outputs: dict | None = None,
    warehouse: dict | None = None,
    project_id: str | None = SCW_PROJECT_ID,
) -> None:
    cloud_ids = {"project": project_id} if project_id else {}
    write_config(root, warehouse=warehouse, environments={"prod": {"cloud_ids": cloud_ids}})
    write_outputs(root, scaleway_outputs() if outputs is None else outputs)


def scaleway_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VECTEN_ENVIRONMENT_FILE", raising=False)
    monkeypatch.delenv("SCW_DEFAULT_PROJECT_ID", raising=False)
    monkeypatch.setenv("SCW_ACCESS_KEY", "SCWTESTACCESSKEY0000")
    monkeypatch.setenv("SCW_SECRET_KEY", SCW_SECRET_KEY)


class FakeSecretManager:
    """The Secret Manager REST calls the runtime provider makes, served from memory.

    The name filter on listing matches by prefix, so a client that trusts it for an exact
    match reads the wrong secret.
    """

    def __init__(self, project_id: str = SCW_PROJECT_ID):
        self.project_id = project_id
        self.secrets: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.failure: tuple[int, bytes] | None = None

    def add(self, path: str, name: str, *values: str, project_id: str | None = None) -> str:
        sid = str(uuid.uuid4())
        self.secrets[sid] = {
            "id": sid,
            "name": name,
            "path": path,
            "project_id": project_id or self.project_id,
            "versions": [v.encode() for v in values],
        }
        return sid

    def value(self, path: str, name: str) -> str | None:
        for secret in self.secrets.values():
            if (secret["path"], secret["name"]) == (path, name) and secret["versions"]:
                return secret["versions"][-1].decode()
        return None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeSecretManager:
        monkeypatch.setattr("vecten_shared.provider.scaleway._http", self)
        return self

    @staticmethod
    def _json(status: int, doc: dict) -> tuple[int, bytes]:
        return status, json.dumps(doc).encode()

    def _not_found(self) -> tuple[int, bytes]:
        return self._json(404, {"type": "not_found", "message": "resource is not found"})

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append((method, url))
        if self.failure is not None:
            return self.failure
        if headers.get("X-Auth-Token") != SCW_SECRET_KEY:
            return self._json(401, {"type": "denied_authentication", "message": "no"})
        parts = urlsplit(url)
        base = urlsplit(SCW_API)
        if parts.netloc != base.netloc or not parts.path.startswith(base.path + "/"):
            return self._not_found()
        route = parts.path[len(base.path) :].split("/")[1:]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if method == "GET" and route == ["secrets"]:
            return self._list(query)
        if len(route) >= 3 and route[0] == "secrets" and route[2] == "versions":
            secret = self.secrets.get(route[1])
            if secret is None:
                return self._not_found()
            if method == "GET" and route[3:] == ["latest_enabled", "access"]:
                if not secret["versions"]:
                    return self._not_found()
                return self._json(
                    200,
                    {
                        "secret_id": secret["id"],
                        "revision": len(secret["versions"]),
                        "data": base64.b64encode(secret["versions"][-1]).decode(),
                    },
                )
            if method == "POST" and route[3:] == []:
                assert headers.get("Content-Type") == "application/json"
                secret["versions"].append(base64.b64decode(json.loads(body)["data"]))
                return self._json(200, {"revision": len(secret["versions"])})
        return self._not_found()

    def _list(self, query: dict[str, str]) -> tuple[int, bytes]:
        found = [
            s
            for s in self.secrets.values()
            if ("project_id" not in query or s["project_id"] == query["project_id"])
            and s["name"].startswith(query.get("name", ""))
            and ("path" not in query or s["path"] == query["path"])
        ]
        page = int(query.get("page", "1"))
        size = int(query.get("page_size", "50"))
        chosen = found[(page - 1) * size : page * size]
        shown = [{k: v for k, v in s.items() if k != "versions"} for s in chosen]
        return self._json(200, {"secrets": shown, "total_count": len(found)})
