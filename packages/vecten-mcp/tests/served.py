"""The MCP server as a client meets it: on the laptop's Postgres, over HTTP, signed in by OAuth.

Shared by the Postgres end-to-end suite, the scripted OAuth client and the scenarios. Nothing
here imports the `vecten` CLI package, which the runtime packages never depend on.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
    )
    from starlette.testclient import TestClient

SHARED_TESTS = Path(__file__).parents[2] / "vecten-shared/tests"
_spec = importlib.util.spec_from_file_location(
    "served_docker_util", SHARED_TESTS / "docker_util.py"
)
docker_util = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = docker_util
_spec.loader.exec_module(docker_util)

# The laptop stack's Postgres command line, as the generated Compose file writes it.
LOG_LINE_PREFIX = "%m [%p] %u@%d app=%a "
PROTOCOL = "2026-07-28"
PUBLIC_URL = "https://mcp.example"
LAPTOP_PASSWORDS = {
    "POSTGRES_PASSWORD": "fixture-master",
    "AIRFLOW_DB_PASSWORD": "fixture-airflow",
    "LOADER_PASSWORD": "fixture-loader",
    "MCP_STATE_DB_PASSWORD": "fixture-state",
    "PROVISIONER_PASSWORD": "fixture-provisioner",
    "IMPERSONATOR_PASSWORD": "fixture-impersonator",
}


@dataclass
class LaptopPg:
    connect: object
    container: str
    host: str
    port: int
    passwords: dict = field(default_factory=lambda: dict(LAPTOP_PASSWORDS))

    def sql(self, statement, *, user="postgres", db="warehouse", params=None):
        """Run one statement with autocommit and return its rows, or None without a result."""
        password = {
            "postgres": self.passwords["POSTGRES_PASSWORD"],
            "loader": self.passwords["LOADER_PASSWORD"],
            "provisioner": self.passwords["PROVISIONER_PASSWORD"],
            "impersonator": self.passwords["IMPERSONATOR_PASSWORD"],
        }[user]
        with self.connect(user, password, db) as conn:
            conn.autocommit = True
            cursor = conn.execute(statement, params)
            return cursor.fetchall() if cursor.description else None

    def rerun_setup(self):
        """Run the init script again, as an administrator does after a new raw schema lands."""
        subprocess.run(
            ["docker", "exec", self.container, "bash", "/docker-entrypoint-initdb.d/10-vecten.sh"],
            check=True,
            capture_output=True,
        )

    def logs(self):
        return subprocess.run(
            ["docker", "logs", self.container], capture_output=True, text=True
        ).stderr


@contextmanager
def laptop_postgres(tmp_path, environment=None):
    """The `local` profile's Postgres: the laptop init script, every database, the log prefix;
    `environment` adds what the generated Compose file passes, such as VECTEN_AREAS."""
    from vecten_shared.sql import local_init_script_path, setup_sql_path

    init = tmp_path / "pg-init"
    (init / "vecten").mkdir(parents=True)
    script = init / "10-vecten.sh"
    shutil.copy(local_init_script_path(), script)
    script.chmod(0o755)
    shutil.copy(setup_sql_path(), init / "vecten" / "warehouse_setup.sql")
    init.chmod(0o755)
    (init / "vecten").chmod(0o755)
    env = {"VECTEN_WITH_MCP": "true", **LAPTOP_PASSWORDS, **(environment or {})}
    command = ["postgres", "-c", f"log_line_prefix={LOG_LINE_PREFIX}"]
    with docker_util.pg_container(init, env, command) as (connect, name):
        with connect("postgres", LAPTOP_PASSWORDS["POSTGRES_PASSWORD"], "postgres") as c:
            host, port = c.info.host, c.info.port
        yield LaptopPg(connect=connect, container=name, host=host, port=port)


def rpc(http, token, method, params=None):
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
    headers["mcp-method"] = method
    if params and "name" in params:
        headers["mcp-name"] = params["name"]
    if token:
        headers["Authorization"] = "Bearer " + token
    return http.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                **(params or {}),
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )


def challenge(verifier):
    from vecten_mcp.auth.service import challenge as pkce

    return pkce(verifier)


def register(http, name="Scripted client", redirect="http://127.0.0.1:9000/cb"):
    response = http.post("/register", json={"client_name": name, "redirect_uris": [redirect]})
    assert response.status_code == 201, response.text
    return response.json()


def browser_consent(http, flow_page, email, password):
    """Sign in on a flow page and approve; returns the consent response."""
    flow = re.search(r'name="flow" value="([^"]+)"', flow_page.text)[1]
    csrf = re.search(r'name="csrf" value="([^"]+)"', flow_page.text)[1]
    consent = http.post(
        "/login", data={"flow": flow, "csrf": csrf, "email": email, "password": password}
    )
    assert "Approve" in consent.text, consent.text[:500]
    return http.post(
        "/consent",
        data={"flow": flow, "csrf": csrf, "decision": "approve"},
        follow_redirects=False,
    )


def human_token(http, resource, email, password, *, client=None):
    """The authorization-code flow with PKCE, as an MCP client runs it; the token response."""
    client = client or register(http)
    verifier = "v" * 43
    page = http.get(
        "/authorize",
        params={
            "client_id": client["client_id"],
            "redirect_uri": client["redirect_uris"][0],
            "response_type": "code",
            "resource": resource,
            "code_challenge": challenge(verifier),
            "code_challenge_method": "S256",
            "state": "scripted",
        },
    )
    assert page.status_code == 200, page.text[:500]
    redirect = browser_consent(http, page, email, password)
    target = unescape(re.search(r'<a href="([^"]+)"', redirect.text)[1])
    code = parse_qs(urlparse(target).query)["code"][0]
    response = http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client["client_id"],
            "redirect_uri": client["redirect_uris"][0],
            "code_verifier": verifier,
            "resource": resource,
        },
    )
    assert response.status_code == 200, response.text
    return {**response.json(), "client": client}


def set_password(runtime, email, password):
    backend = runtime.oauth.backend
    backend.set_password(backend.setup_link(email), password)


class Served(SimpleNamespace):
    """The served app with the calls the suites make; tokens are plain bearer strings."""

    def call_raw(self, token, tool, arguments=None):
        response = rpc(self.http, token, "tools/call", {"name": tool, "arguments": arguments or {}})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def call(self, token, tool, arguments=None):
        result = self.call_raw(token, tool, arguments)
        assert not result.get("isError"), result
        # A tool returning a list may arrive as one text item per element; the structured
        # content, when the server sends it, holds the value whole.
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        if structured is not None:
            return structured
        values = [json.loads(item["text"]) for item in result["content"]]
        return values[0] if len(values) == 1 else values

    def tools(self, token):
        response = rpc(self.http, token, "tools/list")
        assert response.status_code == 200, response.text
        return sorted(tool["name"] for tool in response.json()["result"]["tools"])

    def admin(self, token, operation, data=None, *, status=200):
        if data is None:
            response = self.http.get(
                f"/api/admin/{operation}", headers={"Authorization": "Bearer " + token}
            )
        else:
            response = self.http.post(
                f"/api/admin/{operation}",
                json=data,
                headers={"Authorization": "Bearer " + token},
            )
        assert response.status_code == status, response.text
        return response.json()

    def sign_in(self, email, password="long-fixture-password"):
        set_password(self.runtime, email, password)
        return human_token(self.http, self.runtime.oauth.resource, email, password)


@contextmanager
def serve(root, config, pg, monkeypatch, *, environ=None, extensions=None):
    """`configured_runtime` over `pg` as the laptop's Compose file wires the MCP container."""
    from vecten_mcp.app import create_app
    from vecten_mcp.extensions import McpExtensions
    from vecten_mcp.runtime_setup import configured_runtime

    class LaptopProvider:
        def connection(self, env, db):
            return SimpleNamespace(host=pg.host, port=pg.port, database=db)

    monkeypatch.setattr("vecten_mcp.runtime_setup.get_provider", lambda *args: LaptopProvider())
    for name, value in {
        "VECTEN_ENV": "local",
        "VECTEN_PROVIDER": "local",
        "VECTEN_MCP_PUBLIC_URL": PUBLIC_URL,
        "VECTEN_MCP_SECRET_DIR": str(Path(root) / ".mcp-secrets"),
        "MCP_STATE_DB_PASSWORD": pg.passwords["MCP_STATE_DB_PASSWORD"],
        "MCP_PROVISIONER_PASSWORD": pg.passwords["PROVISIONER_PASSWORD"],
        "MCP_IMPERSONATOR_PASSWORD": pg.passwords["IMPERSONATOR_PASSWORD"],
        **(environ or {}),
    }.items():
        monkeypatch.setenv(name, value)
    runtime = configured_runtime(
        Path(root), config, extensions or McpExtensions.from_config(config)
    )
    with TestClient(create_app(runtime=runtime), base_url=PUBLIC_URL) as http:
        yield Served(runtime=runtime, http=http, pg=pg)
