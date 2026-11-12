"""The VM agent: `ship` installs a release, `boot` restarts it after a reboot, `watch` pulls one.

On AWS, `vecten ship` runs `ship` on every VM through SSM. Scaleway has no run-command API,
so a timer runs `watch`, which pulls the release a pointer object names and runs the
release's own `ship`.

Runs on the image's system python3 (3.9 on Amazon Linux 2023, 3.12 on Ubuntu 24.04) with the
stdlib only. Every external command goes through `run()` and every HTTP request through
`http()`. No secret value reaches an argv, a URL, an error message or a file other than the
run files and the Scaleway key file: values travel in child environments, on stdin, and in
request headers and bodies.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http import client as http_client
from pathlib import Path
from typing import TYPE_CHECKING

# Annotations stay strings under the future import, so 3.10 syntax in them runs on 3.9.
if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    Run = Callable[..., subprocess.CompletedProcess[str]]
    Http = Callable[..., tuple[int, bytes]]

RUN_DIR = Path("/run/vecten")
RELEASES = Path("/opt/vecten/releases")
LETSENCRYPT = Path("/etc/letsencrypt")
# The Scaleway key file, the ack marker and the last pointer handled. The key has to outlive
# a reboot, because deploy removes it from the metadata server once the watcher acknowledges it.
STATE_DIR = Path("/var/lib/vecten")
KEY_FILE_NAME = "scw-key"
ACK_MARKER_NAME = "scw-key.acked"
HANDLED_NAME = "watch-handled.json"
RECREATE_NAME = "recreate-pending"
WATCH_CONFIG = Path("/etc/vecten/watch.json")
AGENT_COPY = Path("/opt/vecten/agent/apply.py")
# Amazon Linux's docker package reads plugins from libexec; Ubuntu's docker.io does not ship
# the Compose and buildx plugins there, so the Scaleway user data installs them under local.
PLUGIN_DIRS = {
    "aws": "/usr/libexec/docker/cli-plugins",
    "scaleway": "/usr/local/lib/docker/cli-plugins",
}
COMPOSE_PLUGIN = "docker-compose"
BUILDX_PLUGIN = "docker-buildx"

SHIP_DIR = Path(".vecten") / "ship"
STACK_DIR = Path(".vecten") / "stack"
# Marks a release a failed ship left `current` pointing at with nothing to roll back to.
NEVER_STARTED = Path(".vecten") / "never-started"
OVERRIDE_FILE = Path("orchestration") / "docker-compose.override.yml"
HOST_KEYS = (
    "host",
    "cloud",
    "components",
    "env",
    "project",
    "region",
    "project_id",
    "bucket",
    "commit",
    "setup_host",
    "hostnames",
    "secret_prefix",
    "compose_project",
    "connector_secrets",
    "private_package_index",
    "compose_plugin_version",
    "buildx_plugin_version",
    "certbot_image",
    "postgres_image",
    "airflow_parallelism",
    "mcp_pool_cap",
    "plugin_dir",
    "service_secrets",
    "service_network",
)
LEGACY_HOST_DEFAULTS = {
    "private_package_index": False,
    "cloud": "aws",
    "project_id": "",
    "plugin_dir": PLUGIN_DIRS["aws"],
    "service_secrets": [],
    "service_network": "",
}
LEGACY_OUTPUT_KEYS = ("bucket", "secret_prefix")
# What the watcher needs before any release exists on the host; user data writes it.
WATCH_KEYS = ("host", "components", "bucket", "region", "project_id", "secret_prefix")

# Each cloud's instance metadata address, where an instance role or user data is served.
METADATA_ADDRESSES = {"aws": "169.254.169.254", "scaleway": "169.254.42.42"}
# A Linux interface name; it is an iptables argument, so it may not start with `-`.
_INTERFACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}")
# Bounded, so a stuck holder of the xtables lock fails the step by name instead of hanging it.
XTABLES_WAIT_SECONDS = "30"
# Every Compose service a binding generates is named with this prefix.
SERVICE_PREFIX = "svc-"

# The metadata server answers user data only to a request from a source port below 1024.
METADATA_KEY_URL = "http://169.254.42.42/user_data/vecten-key"
PRIVILEGED_PORTS = range(1, 1024)
PRIVILEGED_PORT_ATTEMPTS = 50
SECRET_MANAGER_API = "https://api.scaleway.com/secret-manager/v1beta1/regions/{region}"
SECRET_PAGE_SIZE = 100
# Scaleway's own name for "the newest version that is not disabled".
CURRENT_REVISION = "latest_enabled"
HTTP_TIMEOUT_SECONDS = 60
STATUS_ATTEMPTS = 3
STATUS_RETRY_SECONDS = 5
SHIP_LINE_MAX = 300
LOG_PREFIX = "vecten agent log: "
LOG_TAIL_LINES = 20
LOG_EXCERPT_MAX = 40
COMPOSE_TAIL_LINES = 20
# Masking shorter values would blank common words in a log line.
MASK_MIN_LENGTH = 8
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_EXIT_CODE = re.compile(r"Exited \((\d+)\)")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_PLAIN_TYPE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_KEY_PART = re.compile(r"[A-Za-z0-9-]{1,128}")
# datetime.UTC exists from 3.11 only, and the agent runs on 3.9.
_UTC = datetime.timezone(datetime.timedelta(0))

AIRFLOW_ADMIN_USER = "admin"
AIRFLOW_BASIC_AUTH_USER = "admin"
AIRFLOW_APP_SECRETS = (
    "AIRFLOW_FERNET_KEY",
    "AIRFLOW_API_SECRET_KEY",
    "AIRFLOW_JWT_SECRET",
    "AIRFLOW_ADMIN_PASSWORD",
    "AIRFLOW_BASIC_AUTH_PASSWORD",
)
# The official images run Airflow as uid 50000 (gid 0) and the NGINX workers as uid 101; a
# 0600 file is readable in the container only when it belongs to that user.
AIRFLOW_UID, AIRFLOW_GID = 50000, 0
NGINX_UID, NGINX_GID = 101, 101

# The setup SQL reads these names through \getenv; the store holds the cloud names.
SETUP_PASSWORDS = {
    "with_airflow": (("AIRFLOW_DB_PASSWORD", "AIRFLOW_DB_PASSWORD"),),
    "with_mcp_state": (("MCP_STATE_DB_PASSWORD", "MCP_STATE_DB_PASSWORD"),),
    "with_warehouse": (("LOADER_PASSWORD", "LOADER_PASSWORD"),),
    "with_mcp": (
        ("PROVISIONER_PASSWORD", "MCP_PROVISIONER_PASSWORD"),
        ("IMPERSONATOR_PASSWORD", "MCP_IMPERSONATOR_PASSWORD"),
    ),
}
SETUP_DATABASES = ("airflow", "mcp_state", "warehouse")
# Postgres's own defaults for the verifiers it computes.
SCRAM_ITERATIONS = 4096
SCRAM_SALT_BYTES = 16

DNS_WINDOW_SECONDS = 300
DNS_POLL_SECONDS = 15
CERTBOT_ATTEMPTS = 3
CERTBOT_RETRY_SECONDS = 30
BOOT_ATTEMPTS = 3
BOOT_RETRY_SECONDS = 20
COMPOSE_RELEASE_URL = "https://github.com/docker/compose/releases/download/v{version}/{asset}"
COMPOSE_ASSET = "docker-compose-linux-x86_64"
BUILDX_RELEASE_URL = "https://github.com/docker/buildx/releases/download/v{version}"
BUILDX_ASSET = "buildx-v{version}.linux-amd64"


class AgentError(Exception):
    """A failure whose message is safe to print: it names, never quotes, a secret.

    `log` holds compose's last output and the failing containers' last log lines, with every
    secret value the agent injected masked; they quote what those printed, so they are
    reported apart.
    """

    def __init__(self, message: str, log: tuple[str, ...] = ()):
        super().__init__(message)
        self.log = tuple(log)


class PrivilegeError(AgentError):
    """The process may not bind the source port the metadata server demands."""


@contextlib.contextmanager
def step(name: str):
    try:
        yield
    except AgentError as exc:
        raise AgentError(f"{name}: {exc}", exc.log) from None
    except Exception as exc:
        # Any other message may quote a value, so only the exception's type is kept.
        raise AgentError(f"{name}: failed with {type(exc).__name__}") from None


def run(
    args: list[str], env: dict[str, str] | None = None, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    child_env = dict(os.environ, **env) if env else None
    return subprocess.run(
        args, env=child_env, input=input, capture_output=True, text=True, check=False
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib's own handler re-sends every header, credentials included, to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _privileged_request(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    parts = urllib.parse.urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    ports = list(PRIVILEGED_PORTS)
    for attempt in range(1, PRIVILEGED_PORT_ATTEMPTS + 1):
        port = ports[secrets.randbelow(len(ports))]
        connection = http_client.HTTPConnection(
            parts.hostname,
            parts.port or 80,
            timeout=HTTP_TIMEOUT_SECONDS,
            source_address=("", port),
        )
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise PrivilegeError(
                    f"binding a source port below 1024 for {parts.hostname} was refused; "
                    "the watcher must run as root"
                ) from None
            # Another process may hold the port, or a closed connection from it may linger.
            busy = exc.errno in (errno.EADDRINUSE, errno.EADDRNOTAVAIL)
            if not busy or attempt == PRIVILEGED_PORT_ATTEMPTS:
                raise
        finally:
            connection.close()
    raise OSError(errno.EADDRINUSE, "no free privileged port")


def http(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    privileged: bool = False,
) -> tuple[int, bytes]:
    """One request; an error status comes back as a status, never as an exception.

    A redirect is a status too, never followed. A network or protocol failure names its
    exception type and the host only, because its text can carry the URL or the bytes the
    server sent. `privileged` binds a source port below 1024, which the metadata server needs.
    """
    host = urllib.parse.urlsplit(url).hostname or "the server"
    try:
        if privileged:
            return _privileged_request(method, url, headers or {}, body)
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()
    except (OSError, http_client.HTTPException) as exc:
        raise AgentError(f"{host} unreachable: {type(exc).__name__}") from None


def _module_http(*args, **kwargs) -> tuple[int, bytes]:
    # Resolved at call time, so a replaced `http` reaches callers that took the default.
    return http(*args, **kwargs)


def sigv4_headers(
    method: str,
    url: str,
    region: str,
    access: str,
    secret: str,
    payload_hash: str,
    now: datetime.datetime,
    headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """The headers that sign an S3 request with AWS Signature Version 4.

    `url` is already percent-encoded once, as S3 wants its canonical path; `headers` are
    extra headers to sign, which the caller sends too. The returned headers are the ones the
    caller adds: the date, the payload hash and the Authorization header.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=_UTC)
    stamp = now.astimezone(_UTC).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    parts = urllib.parse.urlsplit(url)
    signed = {"host": parts.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp}
    for name, value in (headers or {}).items():
        signed[name.lower()] = " ".join(str(value).split())
    names = sorted(signed)
    query = sorted(
        (urllib.parse.quote(k, safe="-_.~"), urllib.parse.quote(v, safe="-_.~"))
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    )
    canonical = "\n".join(
        [
            method,
            parts.path or "/",
            "&".join(f"{k}={v}" for k, v in query),
            "".join(f"{n}:{signed[n]}\n" for n in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = f"AWS4{secret}".encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": stamp,
        "x-amz-content-sha256": payload_hash,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access}/{scope},"
            f"SignedHeaders={';'.join(names)},Signature={signature}"
        ),
    }


def s3_endpoint(region: str) -> str:
    return f"https://s3.{region}.scw.cloud"


def s3_object_url(region: str, bucket: str, key: str) -> str:
    """Path-style, the key percent-encoded once outside `/` and the unreserved characters."""
    return f"{s3_endpoint(region)}/{bucket}/{urllib.parse.quote(key, safe='/')}"


def scope_for(name: str) -> str:
    for prefix, scope in (
        ("MCP_", "mcp"),
        ("DEPLOY_", "deploy"),
        ("ADMIN_", "admin"),
        ("SETUP_", "setup"),
    ):
        if name.startswith(prefix):
            return scope
    return "platform"


def setup_flags(instance_databases: list[str], with_mcp: bool) -> dict[str, str]:
    unknown = sorted(set(instance_databases) - set(SETUP_DATABASES))
    if unknown:
        raise AgentError(f"no setup for database(s): {', '.join(unknown)}")

    def flag(on: bool) -> str:
        return "true" if on else "false"

    with_warehouse = "warehouse" in instance_databases
    return {
        "with_airflow": flag("airflow" in instance_databases),
        "with_mcp_state": flag("mcp_state" in instance_databases),
        "with_warehouse": flag(with_warehouse),
        "with_mcp": flag(with_mcp and with_warehouse),
    }


def load_host(release: Path) -> dict:
    path = release / SHIP_DIR / "host.json"
    try:
        host = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentError(f"cannot read {path}: {type(exc).__name__}") from None
    if not isinstance(host, dict):
        raise AgentError(f"{path} is not a JSON object")
    # A release built before the cloud keys existed is an AWS release, and rollback and
    # recovery must still start it.
    for key, value in LEGACY_HOST_DEFAULTS.items():
        host.setdefault(key, value)
    from_outputs = [k for k in LEGACY_OUTPUT_KEYS if k not in host]
    if from_outputs and "env" in host:
        outputs = load_outputs(release, host["env"])
        for key in from_outputs:
            if key in outputs:
                host[key] = outputs[key]
    missing = [k for k in HOST_KEYS if k not in host]
    if missing:
        raise AgentError(f"{path} lacks {', '.join(missing)}")
    host["release"] = str(release)
    return host


def load_outputs(release: Path, env: str) -> dict:
    path = release / ".vecten" / "environments" / f"{env}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentError(f"cannot read {path}: {type(exc).__name__}") from None


def project_components(outputs: dict) -> set:
    return {c for h in outputs["hosts"].values() for c in h["components"]}


def _holds(outputs: dict, database: str) -> bool:
    return any(database in p["databases"] for p in outputs["postgres"].values())


def generated_names(outputs: dict) -> list[str]:
    """The credentials the setup host creates; the IAM role grants exactly these names."""
    components = project_components(outputs)
    warehouse = _holds(outputs, "warehouse")
    names = []
    if "platform" in components:
        names.append("AIRFLOW_DB_PASSWORD")
    if warehouse:
        names.append("LOADER_PASSWORD")
    if "mcp" in components:
        names.append("MCP_STATE_DB_PASSWORD")
        if warehouse:
            names += ["MCP_PROVISIONER_PASSWORD", "MCP_IMPERSONATOR_PASSWORD"]
    return names


class Backend:
    """Where a host's secrets and objects live; `backend_for` picks one by host.json `cloud`.

    Secret names are logical (`AIRFLOW_DB_PASSWORD`); a reference is the cloud's own handle
    on a secret, as the outputs file gives a master secret.
    """

    # The key the containers sign with; None where the cloud gives them an identity instead.
    container_key: tuple[str, str] | None = None

    def describe(self, name: str) -> str:
        raise NotImplementedError

    def read_secret(self, name: str) -> str | None:
        raise NotImplementedError

    def read_reference(self, reference: str) -> str | None:
        raise NotImplementedError

    def secret_has_value(self, name: str) -> bool:
        return self.read_secret(name) is not None

    def add_secret_version(self, name: str, value: str) -> None:
        raise NotImplementedError

    def get_object(self, key: str) -> bytes | None:
        raise NotImplementedError

    def put_object(self, key: str, data: bytes) -> None:
        raise NotImplementedError


class AwsBackend(Backend):
    """Secrets Manager through the `aws` CLI, with the instance role's credentials."""

    def __init__(self, region: str, secret_prefix: str, run: Run):
        self.region = region
        self.secret_prefix = secret_prefix
        self.run = run

    def _cli(self, *args: str) -> list[str]:
        return ["aws", "--region", self.region, "secretsmanager", *args]

    def describe(self, name: str) -> str:
        return f"{self.secret_prefix}{scope_for(name)}/{name}"

    def read_reference(self, reference: str) -> str | None:
        """The value of `reference` (a name or an ARN), or None when it holds no value."""
        result = self.run(
            self._cli("get-secret-value", "--secret-id", reference)
            + ["--query", "SecretString", "--output", "text"]
        )
        if result.returncode == 0:
            return result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout
        if "ResourceNotFoundException" in result.stderr:
            return None
        raise AgentError(f"reading {reference} failed (exit {result.returncode})")

    def read_secret(self, name: str) -> str | None:
        return self.read_reference(self.describe(name))

    def add_secret_version(self, name: str, value: str) -> None:
        sid = self.describe(name)
        created = self.run(
            self._cli("create-secret", "--name", sid, "--secret-string", "file:///dev/stdin"),
            input=value,
        )
        if created.returncode == 0:
            return
        if "ResourceExistsException" not in created.stderr:
            raise AgentError(f"creating {sid} failed (exit {created.returncode})")
        if name in AIRFLOW_APP_SECRETS:
            # The platform host's role may create these secrets but not write a version.
            raise AgentError(
                f"{sid} exists without a value; delete it without recovery so the next ship "
                "creates it"
            )
        # The secret exists without a current value, so the value goes in as its first version.
        put = self.run(
            self._cli(
                "put-secret-value", "--secret-id", sid, "--secret-string", "file:///dev/stdin"
            ),
            input=value,
        )
        if put.returncode != 0:
            raise AgentError(f"storing {sid} failed (exit {put.returncode})")

    def get_object(self, key: str) -> bytes | None:
        raise AgentError("on AWS, releases arrive through SSM; the agent reads no objects")

    def put_object(self, key: str, data: bytes) -> None:
        raise AgentError("on AWS, releases arrive through SSM; the agent writes no objects")


def _error_type(body: bytes) -> str:
    """Scaleway's error `type` token, never its message, which can quote the request."""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return ""
    kind = doc.get("type") if isinstance(doc, dict) else None
    return f" {kind}" if isinstance(kind, str) and _PLAIN_TYPE.fullmatch(kind) else ""


def _json_object(raw: bytes, operation: str) -> dict:
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        doc = None
    if not isinstance(doc, dict):
        raise AgentError(f"Secret Manager {operation} answered with no JSON object")
    return doc


class ScalewayBackend(Backend):
    """Secret Manager over REST and Object Storage over signed S3 requests, with the VM's key.

    A logical name `NAME` of scope `s` is the secret named `NAME` at path `<prefix><s>`; the
    prefix is the outputs file's `/vecten/<project>/<env>/`, the layout
    `vecten_shared.provider.scaleway` reads and the host IAM conditions match. A secret is
    found by listing its path and name, then read by ID, as the runtime provider does.
    """

    def __init__(
        self,
        key: tuple[str, str],
        region: str,
        project_id: str,
        secret_prefix: str,
        bucket: str,
        http: Http | None = None,
    ):
        self.access, self.secret = key
        self.container_key = (self.access, self.secret)
        self.region = region
        self.project_id = project_id
        self.secret_prefix = secret_prefix
        self.bucket = bucket
        self._http = http

    def _send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None = None
    ) -> tuple[int, bytes]:
        send = self._http if self._http is not None else _module_http
        return send(method, url, headers, body)

    def secret_location(self, name: str) -> tuple[str, str]:
        return f"{self.secret_prefix.rstrip('/')}/{scope_for(name)}", name

    def describe(self, name: str) -> str:
        path, plain = self.secret_location(name)
        return f"{path}/{plain}"

    def _api(
        self, method: str, path: str, query: dict | None = None, payload: dict | None = None
    ) -> tuple[int, bytes]:
        url = SECRET_MANAGER_API.format(region=urllib.parse.quote(self.region, safe="")) + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"X-Auth-Token": self.secret, "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        return self._send(method, url, headers, body)

    def _failure(self, operation: str, subject: str, status: int, raw: bytes) -> AgentError:
        return AgentError(
            f"Secret Manager {operation} for {subject} answered HTTP {status}{_error_type(raw)}"
        )

    def _secret_id(self, name: str) -> str | None:
        path, plain = self.secret_location(name)
        page, seen = 1, 0
        while True:
            query = {
                "project_id": self.project_id,
                "path": path,
                "name": plain,
                "page": page,
                "page_size": SECRET_PAGE_SIZE,
            }
            status, raw = self._api("GET", "/secrets", query=query)
            if status != 200:
                raise self._failure("ListSecrets", self.describe(name), status, raw)
            doc = _json_object(raw, "ListSecrets")
            batch = [s for s in doc.get("secrets") or [] if isinstance(s, dict)]
            # The name filter is not documented as exact, so the match is made here.
            for entry in batch:
                if entry.get("path") == path and entry.get("name") == plain and entry.get("id"):
                    return str(entry["id"])
            seen += len(batch)
            total = doc.get("total_count")
            if not batch or not isinstance(total, int) or seen >= total:
                return None
            page += 1

    def _access(self, secret_id: str, subject: str) -> str | None:
        status, raw = self._api(
            "GET",
            f"/secrets/{urllib.parse.quote(secret_id, safe='')}/versions/{CURRENT_REVISION}/access",
        )
        # A shell Terraform created with no version yet answers not found.
        if status == 404:
            return None
        if status != 200:
            raise self._failure("AccessSecretVersion", subject, status, raw)
        doc = _json_object(raw, "AccessSecretVersion")
        try:
            return base64.b64decode(doc.get("data") or "", validate=True).decode("utf-8")
        except (ValueError, TypeError):
            raise AgentError(f"{subject} holds no text value") from None

    def read_secret(self, name: str) -> str | None:
        sid = self._secret_id(name)
        return None if sid is None else self._access(sid, self.describe(name))

    def read_reference(self, reference: str) -> str | None:
        return self._access(reference, f"secret {reference}")

    def add_secret_version(self, name: str, value: str) -> None:
        sid = self._secret_id(name)
        # Terraform creates every secret a VM writes, and the VM key may only add versions.
        if sid is None:
            raise AgentError(
                f"{self.describe(name)} does not exist; run `vecten deploy` to create it"
            )
        payload = {"data": base64.b64encode(value.encode("utf-8")).decode("ascii")}
        status, raw = self._api(
            "POST", f"/secrets/{urllib.parse.quote(sid, safe='')}/versions", payload=payload
        )
        if status not in (200, 201):
            raise self._failure("CreateSecretVersion", self.describe(name), status, raw)

    def _object(self, method: str, key: str, data: bytes = b"") -> tuple[int, bytes]:
        url = s3_object_url(self.region, self.bucket, key)
        headers = sigv4_headers(
            method,
            url,
            self.region,
            self.access,
            self.secret,
            hashlib.sha256(data).hexdigest(),
            datetime.datetime.now(_UTC),
        )
        if method == "PUT":
            headers["Content-Type"] = "application/octet-stream"
            return self._send(method, url, headers, data)
        return self._send(method, url, headers)

    def get_object(self, key: str) -> bytes | None:
        status, raw = self._object("GET", key)
        if status == 200:
            return raw
        # Hosts may list the bucket, so a missing key answers 404 rather than 403.
        if status == 404:
            return None
        raise AgentError(f"reading s3://{self.bucket}/{key} answered HTTP {status}")

    def put_object(self, key: str, data: bytes) -> None:
        status, _ = self._object("PUT", key, data)
        if status not in (200, 201, 204):
            raise AgentError(f"writing s3://{self.bucket}/{key} answered HTTP {status}")


def parse_key(text: str, source: str) -> tuple[str, str]:
    """`<access key>:<secret key>`; a malformed value is named by `source`, never quoted."""
    access, _, secret = text.strip().partition(":")
    if not (_KEY_PART.fullmatch(access) and _KEY_PART.fullmatch(secret)):
        raise AgentError(f"{source} does not hold <access key>:<secret key>")
    return access, secret


def read_key_file() -> tuple[str, str]:
    path = STATE_DIR / KEY_FILE_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AgentError(
            f"{path} is missing; the watcher writes it once deploy has placed vecten-key"
        ) from None
    except OSError as exc:
        raise AgentError(f"cannot read {path}: {type(exc).__name__}") from None
    return parse_key(text, str(path))


def backend_for(host: dict, run: Run, http: Http | None = None) -> Backend:
    cloud = host["cloud"]
    if cloud == "aws":
        return AwsBackend(host["region"], host["secret_prefix"], run)
    if cloud == "scaleway":
        return ScalewayBackend(
            read_key_file(),
            host["region"],
            host["project_id"],
            host["secret_prefix"],
            host["bucket"],
            http,
        )
    raise AgentError(f"host.json names cloud {cloud!r}, which this agent has no backend for")


def _new_value(name: str) -> str:
    if name == "AIRFLOW_FERNET_KEY":
        return base64.urlsafe_b64encode(os.urandom(32)).decode()
    return secrets.token_urlsafe(32)


def ensure_generated(host: dict, outputs: dict, run: Run, backend: Backend | None = None) -> None:
    backend = backend or backend_for(host, run)
    names = []
    if host["setup_host"]:
        names += generated_names(outputs)
    if "platform" in host["components"]:
        names += list(AIRFLOW_APP_SECRETS)
    for name in names:
        if not backend.secret_has_value(name):
            backend.add_secret_version(name, _new_value(name))


def _instance(outputs: dict, database: str) -> dict:
    for instance in outputs["postgres"].values():
        if database in instance["databases"]:
            return instance
    raise AgentError(f"no instance in the outputs holds {database}")


def _required(backend: Backend, name: str) -> str:
    value = backend.read_secret(name)
    if value is None:
        raise AgentError(f"{backend.describe(name)} has no value")
    if name == "MCP_IMPERSONATOR_PASSWORD" and value.startswith('{"current":'):
        try:
            current = json.loads(value)["current"]
            if not isinstance(current, str) or not current:
                raise ValueError()
            return current
        except (ValueError, KeyError, TypeError):
            raise AgentError("invalid impersonator rotation envelope") from None
    return value


def secret_env(
    host: dict, outputs: dict, run: Run, backend: Backend | None = None
) -> dict[str, str]:
    backend = backend or backend_for(host, run)
    env = {}
    components = host["components"]
    if "platform" in components:
        for name in ("AIRFLOW_DB_PASSWORD", *AIRFLOW_APP_SECRETS):
            env[name] = _required(backend, name)
        if _holds(outputs, "warehouse"):
            env["LOADER_PASSWORD"] = _required(backend, "LOADER_PASSWORD")
        # Absent optional secrets stay unset: the stack starts and the run names them.
        for name in (*host["connector_secrets"], "ALERT_WEBHOOK_URL"):
            value = backend.read_secret(name)
            if value is not None:
                env[name] = value
        state = _instance(outputs, "airflow")
        password = urllib.parse.quote(env["AIRFLOW_DB_PASSWORD"], safe="")
        env["AIRFLOW_DATABASE_URL"] = (
            f"postgresql+psycopg2://airflow:{password}@{state['host']}:{state['port']}"
            "/airflow?sslmode=require"
        )
    if "mcp" in components:
        env["MCP_STATE_DB_PASSWORD"] = _required(backend, "MCP_STATE_DB_PASSWORD")
    requested = set(env) | set(host["connector_secrets"] if "platform" in components else ())
    # Optional like connector secrets: an unset name reaches its service empty.
    for name in host["service_secrets"]:
        if name not in requested:
            value = backend.read_secret(name)
            if value is not None:
                env[name] = value
    if host.get("private_package_index", False):
        if "platform" in components:
            env["PACKAGE_INDEX_URL"] = _required(backend, "PACKAGE_INDEX_URL")
        if "mcp" in components:
            env["MCP_PACKAGE_INDEX_URL"] = _required(backend, "MCP_PACKAGE_INDEX_URL")
    return env


def stack_env(host: dict, key: tuple[str, str] | None = None) -> dict[str, str]:
    """The variables the generated Compose file references, for Compose's own environment.

    On Scaleway the containers have no instance identity, so they get the VM's key: the
    runtime provider reads Secret Manager with `SCW_*`, boto3 reaches Object Storage with the
    `AWS_*` pair and the S3 endpoint, and Airflow's remote logging takes the same through its
    `aws_default` connection.
    """
    bucket, region = host["bucket"], host["region"]
    env = {
        "VECTEN_PROVIDER": host["cloud"],
        "VECTEN_ENV": host["env"],
        "AWS_REGION": region,
        "VECTEN_BUNDLE_COMMIT": host["commit"],
        "VECTEN_ARTIFACT_URL": f"s3://{bucket}/platform/",
        "AIRFLOW_REMOTE_BASE_LOG_FOLDER": f"s3://{bucket}/platform/logs",
        "AIRFLOW_PARALLELISM": str(host["airflow_parallelism"]),
        "VECTEN_MCP_POOL_CAP": str(host["mcp_pool_cap"]),
        "AIRFLOW_ADMIN_USER": AIRFLOW_ADMIN_USER,
    }
    if host["cloud"] == "scaleway":
        if key is None:
            raise AgentError("a Scaleway stack needs the VM's key")
        access, secret = key
        endpoint = s3_endpoint(region)
        connection = {
            "conn_type": "aws",
            "login": access,
            "password": secret,
            "extra": {"endpoint_url": endpoint, "region_name": region},
        }
        env.update(
            {
                "SCW_ACCESS_KEY": access,
                "SCW_SECRET_KEY": secret,
                "SCW_DEFAULT_REGION": region,
                "SCW_DEFAULT_PROJECT_ID": host["project_id"],
                "AWS_ACCESS_KEY_ID": access,
                "AWS_SECRET_ACCESS_KEY": secret,
                "AWS_ENDPOINT_URL_S3": endpoint,
                "AIRFLOW_CONN_AWS_DEFAULT": json.dumps(connection),
            }
        )
    return env


def isolate_services(host: dict, run: Run) -> None:
    """Keep containers on the services bridge off the metadata address, before they start.

    A reboot clears the rule, so boot inserts it again; `-C` first keeps one rule however
    often this runs.
    """
    bridge = host["service_network"]
    if not bridge:
        return
    if not _INTERFACE.fullmatch(bridge):
        raise AgentError(f"service_network {bridge!r} is no interface name")
    address = METADATA_ADDRESSES.get(host["cloud"])
    if address is None:
        raise AgentError(f"no metadata address known for cloud {host['cloud']!r}")
    rule = ["DOCKER-USER", "-i", bridge, "-d", address, "-j", "DROP"]
    iptables = ["iptables", "-w", XTABLES_WAIT_SECONDS]
    if run([*iptables, "-C", *rule]).returncode == 0:
        return
    result = run([*iptables, "-I", *rule])
    if result.returncode != 0:
        raise AgentError(
            f"inserting the DOCKER-USER rule for {bridge} failed (exit {result.returncode})"
        )


def stop_services(host: dict, run: Run) -> None:
    """Stop the project's service containers; never raises, the failure that called it does."""
    try:
        listing = run(
            ["docker", "ps", "--format", '{{.ID}}\t{{.Label "com.docker.compose.service"}}']
            + ["--filter", f"label=com.docker.compose.project={host['compose_project']}"]
        )
        rows = [row.split("\t") for row in listing.stdout.splitlines()]
        ids = [r[0] for r in rows if len(r) > 1 and r[1].startswith(SERVICE_PREFIX)]
        if ids:
            run(["docker", "stop", *ids])
    except Exception:
        return


def scram_verifier(password: str, salt: bytes | None = None) -> str:
    """The SCRAM-SHA-256 verifier Postgres stores for `password` (RFC 5802, RFC 7677).

    The same algorithm as `vecten.deploy.admin.scram_verifier`, which this stdlib-only agent
    cannot import. Postgres stores a verifier given as a role's password unchanged, so the
    plaintext never reaches the server or its statement logs. The generated passwords are
    ASCII, which SASLprep leaves unchanged.
    """
    salt = salt if salt is not None else os.urandom(SCRAM_SALT_BYTES)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, SCRAM_ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode()

    return f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def run_setup(host: dict, outputs: dict, run: Run, backend: Backend | None = None) -> None:
    backend = backend or backend_for(host, run)
    with_mcp = "mcp" in project_components(outputs)
    sql_dir = Path(host["release"]) / SHIP_DIR
    for key in sorted(outputs["postgres"]):
        instance = outputs["postgres"][key]
        raw = backend.read_reference(instance["master_secret"])
        if raw is None:
            raise AgentError(f"the {key} instance's master secret has no value")
        try:
            credential = json.loads(raw)
            user, password = credential["username"], credential["password"]
        except (ValueError, KeyError, TypeError):
            raise AgentError(
                f"the {key} instance's master secret is not username/password JSON"
            ) from None
        flags = setup_flags(instance["databases"], with_mcp)
        env = {"PGPASSWORD": password, "PGSSLMODE": "require", "MASTER_ROLE": user}
        for flag, pairs in SETUP_PASSWORDS.items():
            if flags[flag] == "true":
                for sql_name, cloud_name in pairs:
                    env[sql_name] = scram_verifier(_required(backend, cloud_name))
        args = ["docker", "run", "--rm", "--network", "host"]
        for name in env:
            args += ["-e", name]
        args += ["-v", f"{sql_dir}:/sql:ro", host["postgres_image"], "psql"]
        args += ["-h", instance["host"], "-p", str(instance["port"]), "-U", user, "-d", "postgres"]
        args += ["-v", "ON_ERROR_STOP=1"]
        for flag, value in flags.items():
            args += ["-v", f"{flag}={value}"]
        args += ["-f", "/sql/warehouse_setup.sql"]
        # psql's output can quote the statements it ran, so only the exit code is reported.
        result = run(args, env=env)
        if result.returncode != 0:
            raise AgentError(f"setup SQL on the {key} instance failed (exit {result.returncode})")


def _nginx_ids(host: dict, run: Run) -> list[str]:
    result = run(
        ["docker", "ps", "-q"]
        + ["--filter", f"label=com.docker.compose.project={host['compose_project']}"]
        + ["--filter", "label=com.docker.compose.service=nginx"]
    )
    return result.stdout.split() if result.returncode == 0 else []


def resolve(hostname: str) -> list[str]:
    """The addresses `hostname` resolves to from this VM, or none."""
    try:
        infos = socket.getaddrinfo(hostname, 80, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return []
    return sorted({info[4][0] for info in infos})


def _await_dns(hostname: str, address: str, sleep: Callable, resolve: Callable) -> None:
    waited = 0
    while True:
        found = resolve(hostname)
        if address in found:
            return
        if waited >= DNS_WINDOW_SECONDS:
            seen = f"resolves to {', '.join(found)}" if found else "does not resolve"
            raise AgentError(
                f"{hostname} {seen}, not this host's {address}, after {DNS_WINDOW_SECONDS} s"
            )
        sleep(DNS_POLL_SECONDS)
        waited += DNS_POLL_SECONDS


def _certbot(host: dict, hostname: str, run: Run, sleep: Callable) -> None:
    args = ["docker", "run", "--rm", "-p", "80:80"]
    args += ["-v", f"{LETSENCRYPT}:/etc/letsencrypt", host["certbot_image"]]
    args += ["certonly", "--standalone", "--non-interactive", "--agree-tos"]
    args += ["--register-unsafely-without-email", "-d", hostname]
    for attempt in range(1, CERTBOT_ATTEMPTS + 1):
        if run(args).returncode == 0:
            return
        if attempt < CERTBOT_ATTEMPTS:
            sleep(CERTBOT_RETRY_SECONDS)
    raise AgentError(f"no certificate for {hostname} after {CERTBOT_ATTEMPTS} attempts")


def ensure_certificates(
    host: dict,
    outputs: dict,
    run: Run,
    sleep: Callable[[float], None],
    resolve: Callable[[str], list[str]] = resolve,
) -> None:
    missing = [
        h for h in host["hostnames"] if not (LETSENCRYPT / "live" / h / "fullchain.pem").is_file()
    ]
    if not missing:
        return
    # Let's Encrypt allows five failed validations per hostname an hour, so certbot runs only
    # once the name already points at this host.
    address = outputs["hosts"][host["host"]]["public_ip"]
    for hostname in missing:
        _await_dns(hostname, address, sleep, resolve)
    # certbot --standalone answers the HTTP challenge itself, so it needs port 80 free.
    ids = _nginx_ids(host, run)
    if ids and run(["docker", "stop", *ids]).returncode != 0:
        raise AgentError("stopping NGINX for certbot failed")
    try:
        for hostname in missing:
            _certbot(host, hostname, run, sleep)
    except AgentError:
        # A re-ship that fails here would otherwise leave the running release unreachable.
        if ids:
            run(["docker", "start", *ids])
        raise


def _checksum_for(listing: str, asset: str) -> str | None:
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset:
            return parts[0]
    return None


def _install_plugin(
    label: str, version: str, url: str, checksums_url: str, asset: str, dest: Path, run: Run
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="vecten-plugin-"))
    try:
        binary, checksums = workdir / asset, workdir / "checksums"
        for path, source in ((binary, url), (checksums, checksums_url)):
            result = run(["curl", "-fsSL", "--retry", "5", "-o", str(path), source])
            if result.returncode != 0:
                raise AgentError(f"downloading {source} failed (exit {result.returncode})")
        expected = _checksum_for(checksums.read_text(encoding="utf-8"), asset)
        if expected != hashlib.sha256(binary.read_bytes()).hexdigest():
            raise AgentError(f"the {label} plugin {version} failed its checksum")
        result = run(["install", "-D", "-m", "0755", str(binary), str(dest)])
        if result.returncode != 0:
            raise AgentError(f"installing the {label} plugin failed (exit {result.returncode})")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def ensure_compose_plugin(host: dict, run: Run) -> None:
    """Cloud-init installs the plugin once; a pin bump reaches the VM only through here."""
    wanted = host["compose_plugin_version"]
    current = run(["docker", "compose", "version", "--short"])
    if current.returncode == 0 and current.stdout.strip().lstrip("v") == wanted:
        return
    url = COMPOSE_RELEASE_URL.format(version=wanted, asset=COMPOSE_ASSET)
    dest = Path(host["plugin_dir"]) / COMPOSE_PLUGIN
    _install_plugin("Compose", wanted, url, f"{url}.sha256", COMPOSE_ASSET, dest, run)


def ensure_buildx_plugin(host: dict, run: Run) -> None:
    """Compose builds through buildx; like Compose, a pin bump arrives only through here."""
    wanted = host["buildx_plugin_version"]
    current = run(["docker", "buildx", "version"])
    # `docker buildx version` prints "github.com/docker/buildx v0.37.2 <commit>".
    words = current.stdout.split()
    if current.returncode == 0 and len(words) > 1 and words[1].lstrip("v") == wanted:
        return
    asset = BUILDX_ASSET.format(version=wanted)
    base = BUILDX_RELEASE_URL.format(version=wanted)
    dest = Path(host["plugin_dir"]) / BUILDX_PLUGIN
    _install_plugin("buildx", wanted, f"{base}/{asset}", f"{base}/checksums.txt", asset, dest, run)


def _write_private(path: Path, text: str, owner: tuple) -> None:
    # Docker creates a directory for a bind-mount source missing when a container starts.
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    # Written in place: a bind mount holds the inode, so a rename would leave running
    # containers on the old file.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    if os.geteuid() == 0:
        os.chown(str(path), *owner)


def write_run_files(host: dict, env: dict[str, str], run_dir: Path) -> None:
    if run_dir.is_file() or run_dir.is_symlink():
        run_dir.unlink()
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    if "platform" not in host["components"]:
        return
    passwords = json.dumps({AIRFLOW_ADMIN_USER: env["AIRFLOW_ADMIN_PASSWORD"]})
    _write_private(run_dir / "airflow_passwords.json", passwords, (AIRFLOW_UID, AIRFLOW_GID))
    digest = hashlib.sha1(env["AIRFLOW_BASIC_AUTH_PASSWORD"].encode("utf-8")).digest()
    line = f"{AIRFLOW_BASIC_AUTH_USER}:{{SHA}}{base64.b64encode(digest).decode()}\n"
    _write_private(run_dir / "htpasswd", line, (NGINX_UID, NGINX_GID))


def _real(path: Path) -> Path:
    return Path(os.path.realpath(str(path)))


def _point(link: Path, target: Path) -> None:
    tmp = link.with_name(f".{link.name}.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    os.replace(str(tmp), str(link))


def swap(releases: Path, new: Path) -> None:
    releases.mkdir(parents=True, exist_ok=True)
    current, previous = releases / "current", releases / "previous"
    new = _real(new)
    old = _real(current) if current.is_symlink() else None
    # A release that never started is no rollback target; it is deleted below instead.
    if old is not None and old != new and not (old / NEVER_STARTED).exists():
        _point(previous, old)
    _point(current, new)
    keep = {new}
    if previous.is_symlink():
        keep.add(_real(previous))
    for child in releases.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if _real(child) not in keep:
            shutil.rmtree(child)


def compose_up(
    release: Path,
    host: dict,
    env: dict[str, str],
    run: Run,
    force_recreate: bool = False,
    secrets: Iterable[str] = (),
) -> None:
    """Start the release's stack; a failure carries the tail of compose's own output, which
    names a failed pull, build or port when no container is running yet."""
    stack = _real(release) / STACK_DIR
    args = ["docker", "compose", "--project-name", host["compose_project"]]
    args += ["--project-directory", str(stack), "-f", str(stack / "docker-compose.yml")]
    override = _real(release) / OVERRIDE_FILE
    if override.is_file():
        # The project's additions merge over the generated file, so they come after it.
        args += ["-f", str(override)]
    for component in host["components"]:
        args += ["--profile", component]
    # Every release tags its project images alike, so `--build` keeps the tag on this one.
    args += ["up", "-d", "--build", "--remove-orphans"]
    if force_recreate:
        args.append("--force-recreate")
    result = run(args, env=env)
    if result.returncode != 0:
        masked = _masked(secrets)
        lines = [_log_line(raw, masked) for raw in (result.stderr or "").splitlines()]
        tail = [f"compose: {line}"[:SHIP_LINE_MAX] for line in lines if line]
        raise AgentError(
            f"docker compose up failed (exit {result.returncode})", tail[-COMPOSE_TAIL_LINES:]
        )


def _failing(state: str, status: str) -> bool:
    if state in ("restarting", "dead") or "(unhealthy)" in status:
        return True
    code = _EXIT_CODE.match(status)
    return state == "exited" and code is not None and code.group(1) != "0"


def _masked(secrets: Iterable[str]) -> list[str]:
    return sorted({v for v in secrets if len(v) >= MASK_MIN_LENGTH}, key=len, reverse=True)


def _log_line(raw: str, secrets: list[str]) -> str:
    line = _CONTROL.sub(" ", _ANSI.sub("", raw)).strip()
    for value in secrets:
        line = line.replace(value, "***")
    return line


def _injected(env: dict[str, str], backend: Backend) -> list[str]:
    """Every secret value the agent hands the containers: the secrets and the VM's key."""
    return [*env.values(), *(backend.container_key or ())]


def container_log_excerpt(host: dict, run: Run, secrets: Iterable[str]) -> list[str]:
    """The last log lines of each container of the project that is failing, `<service>: <line>`.

    Never raises: it runs while a ship is already failing, and that failure is the one the
    operator acts on.
    """
    masked = _masked(secrets)
    excerpt: list[str] = []
    try:
        listing = run(
            ["docker", "ps", "-a"]
            + ["--filter", f"label=com.docker.compose.project={host['compose_project']}"]
            + [
                "--format",
                '{{.ID}}\t{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}',
            ]
        )
        if listing.returncode != 0:
            return excerpt
        for row in listing.stdout.splitlines():
            fields = row.split("\t")
            if len(fields) != 4 or not _failing(fields[2], fields[3]):
                continue
            ident, service = fields[0], fields[1]
            logs = run(["docker", "logs", "--tail", str(LOG_TAIL_LINES), ident])
            output = "\n".join((logs.stdout or "", logs.stderr or ""))
            lines = [_log_line(raw, masked) for raw in output.splitlines()]
            for line in [ln for ln in lines if ln][-LOG_TAIL_LINES:]:
                if len(excerpt) == LOG_EXCERPT_MAX:
                    return excerpt
                excerpt.append(f"{service}: {line}"[:SHIP_LINE_MAX])
    except Exception:
        return excerpt
    return excerpt


def _start(
    release: Path,
    run: Run,
    run_dir: Path,
    force_recreate: bool,
    attempts: int = 1,
    sleep: Callable[[float], None] = time.sleep,
    http: Http | None = None,
    excerpt: bool = False,
) -> list[str]:
    """Start `release`; with `excerpt`, the failing containers' log lines taken before
    `compose up` replaces them."""
    with step("release"):
        host = load_host(release)
        outputs = load_outputs(release, host["env"])
    # Docker restarts running services with the daemon, before this runs; the rule needs no
    # secret, so it comes first, and services that cannot have it are stopped.
    try:
        with step("service isolation"):
            isolate_services(host, run)
    except AgentError:
        stop_services(host, run)
        raise
    for attempt in range(1, attempts + 1):
        try:
            with step("read secrets"):
                backend = backend_for(host, run, http)
                env = secret_env(host, outputs, run, backend)
            with step("run files"):
                write_run_files(host, env, run_dir)
            break
        except AgentError:
            if attempt == attempts:
                raise
            sleep(BOOT_RETRY_SECONDS)
    injected = _injected(env, backend)
    log = container_log_excerpt(host, run, injected) if excerpt else []
    try:
        with step("compose up"):
            stack = stack_env(host, backend.container_key)
            compose_up(release, host, {**stack, **env}, run, force_recreate, injected)
    except AgentError as exc:
        raise AgentError(str(exc), (list(exc.log) + log)[:LOG_EXCERPT_MAX]) from None
    return log


def ship(
    release: Path,
    run: Run,
    releases: Path | None = None,
    run_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    resolve: Callable[[str], list[str]] = resolve,
) -> None:
    with step("release"):
        host = load_host(release)
        outputs = load_outputs(release, host["env"])
    with step("compose plugin"):
        ensure_compose_plugin(host, run)
    with step("buildx plugin"):
        ensure_buildx_plugin(host, run)
    with step("generated secrets"):
        backend = backend_for(host, run)
        ensure_generated(host, outputs, run, backend)
    if host["setup_host"]:
        with step("setup SQL"):
            run_setup(host, outputs, run, backend)
    with step("read secrets"):
        env = secret_env(host, outputs, run, backend)
    with step("certificates"):
        ensure_certificates(host, outputs, run, sleep, resolve)
    try:
        with step("run files"):
            write_run_files(host, env, run_dir or RUN_DIR)
        with step("service isolation"):
            isolate_services(host, run)
    except AgentError:
        # The certificate step may have stopped NGINX, and the running release still serves.
        _ensure_nginx(host, run)
        raise
    releases = releases or RELEASES
    current = releases / "current"
    # A re-shipped release was extracted over itself, so files its containers bind-mount may
    # have new inodes that Compose would not notice without recreating them.
    reshipped = current.is_symlink() and _real(current) == _real(release)
    try:
        with step("swap"):
            swap(releases, release)
        with step("compose up"):
            stack = stack_env(host, backend.container_key)
            compose_up(
                release,
                host,
                {**stack, **env},
                run,
                force_recreate=reshipped,
                secrets=_injected(env, backend),
            )
    except AgentError as exc:
        # The recovery recreates the containers, and their logs with them.
        log = list(exc.log) + container_log_excerpt(host, run, _injected(env, backend))
        outcome = _recover(host, releases, release, run, run_dir)
        raise AgentError(f"{exc}; {outcome}", log[:LOG_EXCERPT_MAX]) from None
    with contextlib.suppress(FileNotFoundError):
        (_real(release) / NEVER_STARTED).unlink()


def _ensure_nginx(host: dict, run: Run) -> None:
    """Start the project's NGINX containers again if a failed ship left them stopped."""
    try:
        result = run(
            ["docker", "ps", "-a", "-q"]
            + ["--filter", f"label=com.docker.compose.project={host['compose_project']}"]
            + ["--filter", "label=com.docker.compose.service=nginx"]
        )
        ids = result.stdout.split() if result.returncode == 0 else []
        if ids:
            run(["docker", "start", *ids])
    except Exception:
        # The ship's own failure is the line the operator acts on; this one must not mask it.
        return


def _recover(host: dict, releases: Path, release: Path, run: Run, run_dir: Path | None) -> str:
    """Put the host back on the release before `release`; the outcome as a clause to report."""
    current = releases / "current"
    try:
        if current.is_symlink() and _real(current) != _real(release):
            # The swap failed before `current` moved, so the running release never changed.
            return f"rolled back to {_real(current).name}"
        previous = releases / "previous"
        if not previous.is_symlink() or not previous.is_dir():
            (_real(release) / NEVER_STARTED).touch()
            return "no previous release to roll back to"
        commit = _real(previous).name
        try:
            rollback(releases, run, run_dir)
        except AgentError as exc:
            return f"rolling back to {commit} failed: {exc}"
        return f"rolled back to {commit}"
    except Exception as exc:
        # Any other message may quote a value, so only the exception's type is kept.
        return f"rolling back failed with {type(exc).__name__}"
    finally:
        _ensure_nginx(host, run)


def rollback(releases: Path, run: Run, run_dir: Path | None = None) -> list[str]:
    """Start the previous release again; the log lines of the containers that were failing."""
    previous = releases / "previous"
    if not previous.is_symlink() or not previous.is_dir():
        raise AgentError("no previous bundle")
    target = _real(previous)
    with step("swap"):
        _point(releases / "current", target)
        previous.unlink()
    return _start(target, run, run_dir or RUN_DIR, force_recreate=True, excerpt=True)


def boot(
    releases: Path,
    run: Run,
    run_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    current = releases / "current"
    if not current.is_dir():
        raise AgentError("no current release")
    # Docker has already restarted the containers against a run directory that was empty.
    # The instance role and Secrets Manager may not answer in the first seconds after boot.
    _start(
        _real(current),
        run,
        run_dir or RUN_DIR,
        force_recreate=True,
        attempts=BOOT_ATTEMPTS,
        sleep=sleep,
    )


def load_watch_config(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentError(f"cannot read {path}: {type(exc).__name__}") from None
    if not isinstance(doc, dict):
        raise AgentError(f"{path} is not a JSON object")
    missing = [k for k in WATCH_KEYS if k not in doc]
    if missing:
        raise AgentError(f"{path} lacks {', '.join(missing)}")
    if not isinstance(doc["components"], list) or not doc["components"]:
        raise AgentError(f"{path} names no components")
    return doc


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write_root_only(path: Path, text: str) -> None:
    """Replace `path` whole: a reader sees the old content or the new, never a partial file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def _metadata_key(send: Http) -> str | None:
    """The `vecten-key` user data entry, or None while deploy has not placed it or has removed it.

    An unreachable metadata server is treated like an absent entry: the key file then serves.
    A refused privileged port is not: without root the watcher could never see a key.
    """
    try:
        status, raw = send("GET", METADATA_KEY_URL, {}, None, privileged=True)
    except PrivilegeError:
        raise
    except AgentError:
        return None
    if status != 200 or not raw.strip():
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AgentError("the vecten-key user data entry is not text") from None


def ensure_key(send: Http, config: dict, state_dir: Path) -> tuple[str, str] | None:
    """The VM's key, kept on disk and acknowledged once per key.

    Deploy removes `vecten-key` from the metadata server once the acknowledgement object
    exists, because a Terraform refresh would copy every user data entry into state; the file
    on disk then serves, through reboots too. A different key in metadata is a rotation: the
    running containers still hold the old key, so a recreate is marked pending.
    """
    key_file = state_dir / KEY_FILE_NAME
    text = _metadata_key(send)
    if text is None:
        stored = _read_text(key_file)
        return None if stored is None else parse_key(stored, str(key_file))
    key = parse_key(text, "the vecten-key user data entry")
    joined = f"{key[0]}:{key[1]}"
    if _read_text(key_file) != joined:
        # Marker first: a crash between the writes leaves a marker and a repeated key write,
        # never a new key that no restart will reach the containers with.
        _write_root_only(state_dir / RECREATE_NAME, "")
        _write_root_only(key_file, joined)
    digest = hashlib.sha256(key[0].encode("utf-8")).hexdigest()
    marker = state_dir / ACK_MARKER_NAME
    if _read_text(marker) != digest:
        store = _watch_backend(config, key, send)
        ack = json.dumps({"host": config["host"]}).encode("utf-8")
        store.put_object(f"{config['components'][0]}/key-ack/{digest}.json", ack)
        _write_root_only(marker, digest)
    return key


def _watch_backend(config: dict, key: tuple[str, str], send: Http) -> ScalewayBackend:
    return ScalewayBackend(
        key, config["region"], config["project_id"], config["secret_prefix"], config["bucket"], send
    )


def read_pointer(store: Backend, host: str) -> dict | None:
    raw = store.get_object(f"bundles/{host}/pointer.json")
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise AgentError("the pointer is not JSON") from None
    commit = doc.get("commit") if isinstance(doc, dict) else None
    # The commit names a directory and an argv element, so it must be a bare object name.
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise AgentError("the pointer names no full git commit id")
    requested = doc.get("requested_at")
    return {
        "commit": commit,
        "requested_at": requested if isinstance(requested, str) else None,
        "force": doc.get("force") is True,
    }


def _last_line(stderr: str) -> str | None:
    for raw in reversed(stderr.splitlines()):
        line = raw.strip()
        if line:
            return line[:SHIP_LINE_MAX]
    return None


def _refresh_agent_copy(source: Path) -> None:
    """The watcher runs the agent of the newest release that shipped, so its fixes arrive."""
    AGENT_COPY.parent.mkdir(parents=True, exist_ok=True)
    tmp = AGENT_COPY.with_name(f".{AGENT_COPY.name}.tmp")
    shutil.copyfile(str(source), str(tmp))
    os.chmod(str(tmp), 0o755)
    os.replace(str(tmp), str(AGENT_COPY))


def _log_lines(stderr: str) -> list[str]:
    return [
        line[len(LOG_PREFIX) :][:SHIP_LINE_MAX]
        for line in stderr.splitlines()
        if line.startswith(LOG_PREFIX)
    ][:LOG_EXCERPT_MAX]


def _pull_and_ship(store: Backend, host: str, releases: Path, commit: str, run: Run):
    """Download, extract and ship `commit`: whether it shipped, the line to report and the
    failing containers' log lines the release's agent printed."""
    key = f"bundles/{host}/{commit}.tar.gz"
    release = releases / commit
    try:
        with step("download"):
            data = store.get_object(key)
        if data is None:
            return False, f"vecten agent watch: bundle {key} is missing", []
        incoming = releases.parent / "incoming"
        archive = incoming / f"{commit}.tar.gz"
        try:
            with step("extract"):
                incoming.mkdir(parents=True, exist_ok=True)
                archive.write_bytes(data)
                current = releases / "current"
                running = current.is_symlink() and _real(current) == _real(release)
                # The running release is extracted over itself, as on AWS: its containers
                # bind-mount files from it, and ship then recreates them.
                if not running and release.is_dir() and not release.is_symlink():
                    shutil.rmtree(release)
                release.mkdir(parents=True, exist_ok=True)
                result = run(["tar", "-xzf", str(archive), "-C", str(release), "--no-same-owner"])
                if result.returncode != 0:
                    raise AgentError(f"tar failed (exit {result.returncode})")
        finally:
            with contextlib.suppress(OSError):
                archive.unlink()
    except AgentError as exc:
        return False, f"vecten agent watch: {exc}", []
    agent = release / SHIP_DIR / "apply.py"
    # Only the agent bundled with a release knows the contract that release was built against.
    result = run([sys.executable, str(agent), "ship", "--release", str(release)])
    if result.returncode != 0:
        line = _last_line(result.stderr) or f"vecten agent ship: failed (exit {result.returncode})"
        return False, line, _log_lines(result.stderr)
    line = f"shipped {commit}"
    try:
        _refresh_agent_copy(agent)
    except OSError as exc:
        line += f"; the watcher's agent copy was not refreshed ({type(exc).__name__})"
    return True, line, []


def _running(releases: Path) -> str | None:
    current = releases / "current"
    return _real(current).name if current.is_symlink() and current.is_dir() else None


def _requested_before_key(wanted: dict, state_dir: Path) -> bool:
    """Whether the pointer was requested before this server acknowledged its key.

    The acknowledgement marker's modification time is the moment the watcher wrote the
    acknowledgement object, and deploy writes its pointer only after seeing that object, so
    an older pointer was meant for a server this one replaced. A pointer without a readable
    `requested_at` was not written by CI and is never taken for such a one.
    """
    try:
        acked = (state_dir / ACK_MARKER_NAME).stat().st_mtime
        # fromisoformat accepts a trailing Z only from 3.11, and the agent runs on 3.9.
        stamp = str(wanted["requested_at"]).replace("Z", "+00:00")
        requested = datetime.datetime.fromisoformat(stamp)
    except (OSError, ValueError):
        return False
    if requested.tzinfo is None:
        return False
    return requested.timestamp() < acked


def _report(
    store: Backend,
    component: str,
    wanted: dict,
    ok: bool,
    line: str,
    log: list[str],
    running,
    sleep,
) -> None:
    """The status object CI polls; a lost one would make CI roll back a release that runs.

    `running` names the release the host runs after the agent finished, any rollback of its
    own included, so CI can point the pointer back at it when the ship failed.
    """
    body = json.dumps(
        {
            "ok": ok,
            "line": line,
            "requested_at": wanted["requested_at"],
            "running": running,
            "log": log,
        }
    )
    key = f"{component}/ship-status/{wanted['commit']}.json"
    for attempt in range(1, STATUS_ATTEMPTS + 1):
        try:
            store.put_object(key, body.encode("utf-8"))
            return
        except AgentError:
            if attempt == STATUS_ATTEMPTS:
                raise
            sleep(STATUS_RETRY_SECONDS)


@contextlib.contextmanager
def _watch_lock(run_dir: Path):
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(run_dir / "watch.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            locked = False
        yield locked
    finally:
        os.close(fd)


def watch(
    releases: Path,
    run: Run,
    http: Http | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """One pull: ship the commit this host's pointer names unless it was handled already.

    A pointer is handled once per `requested_at`, so CI's rollback, which points back at the
    running release, gets a status of its own. Returns the line to log.
    """
    send = http if http is not None else _module_http
    with _watch_lock(RUN_DIR) as locked:
        if not locked:
            return "another watch run holds the lock"
        with step("watch config"):
            config = load_watch_config(WATCH_CONFIG)
        with step("key"):
            key = ensure_key(send, config, STATE_DIR)
        if key is None:
            return "waiting for key: deploy has not placed vecten-key in this server's user data"
        current = releases / "current"
        pending = STATE_DIR / RECREATE_NAME
        store = _watch_backend(config, key, send)
        with step("pointer"):
            wanted = read_pointer(store, config["host"])
        handled_file = STATE_DIR / HANDLED_NAME
        try:
            handled = json.loads(_read_text(handled_file) or "null")
        except ValueError:
            handled = None
        fresh = wanted is not None and handled != wanted
        is_current = (
            wanted is not None and current.is_symlink() and _real(current).name == wanted["commit"]
        )
        to_ship = fresh and (not is_current or wanted["force"])
        # A ship starts its stack on the key file, so a pending recreate waits for it rather
        # than restart the stack twice, and a recreate that keeps failing never blocks a ship.
        if pending.exists() and not to_ship:
            # The marker goes only once the stack runs on the new key, so a failure retries.
            if current.is_dir():
                with step("recreate with the rotated key"):
                    _start(_real(current), run, RUN_DIR, force_recreate=True, http=send)
            pending.unlink()
        if wanted is None:
            return "no release requested"
        if not fresh:
            return f"nothing to do: {wanted['commit']} was handled"
        # A replaced server starts with no release and finds the pointer its predecessor
        # shipped; deploy writes a newer one once this server holds its key.
        if not current.is_dir() and _requested_before_key(wanted, STATE_DIR):
            return (
                f"waiting for a release requested after this server's key: the pointer to "
                f"{wanted['commit']} predates it"
            )
        if to_ship:
            ok, line, log = _pull_and_ship(store, config["host"], releases, wanted["commit"], run)
            if ok:
                with contextlib.suppress(FileNotFoundError):
                    pending.unlink()
        else:
            ok, line, log = True, f"{wanted['commit']} is already the current release", []
        with step("status"):
            _report(
                store, config["components"][0], wanted, ok, line, log, _running(releases), sleep
            )
        _write_root_only(handled_file, json.dumps(wanted))
        return line


def main(argv: list[str] | None = None, run: Run = run) -> int:
    parser = argparse.ArgumentParser(prog="apply.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    ship_cmd = commands.add_parser("ship", help="install a release and start it")
    ship_cmd.add_argument("--release", required=True, type=Path)
    commands.add_parser("rollback", help="start the previous release again")
    commands.add_parser("boot", help="rebuild the run files and start the current release")
    commands.add_parser("watch", help="ship the release this host's pointer object names")
    args = parser.parse_args(argv)
    try:
        if args.command == "ship":
            ship(args.release, run)
        elif args.command == "rollback":
            for line in rollback(RELEASES, run):
                print(LOG_PREFIX + line, file=sys.stderr)
        elif args.command == "watch":
            print(f"vecten agent watch: {watch(RELEASES, run)}")
        else:
            boot(RELEASES, run)
    except AgentError as exc:
        for line in exc.log:
            print(LOG_PREFIX + line, file=sys.stderr)
        print(f"vecten agent {args.command}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Any other message may quote a value, so only the exception's type is printed.
        print(f"vecten agent {args.command}: failed with {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
