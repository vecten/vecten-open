"""CLI authentication cache and HTTP administration; no server imports."""

import json
import os
import ssl
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from filelock import FileLock, Timeout

from vecten_shared.config import load_config
from vecten_shared.issues import cap
from vecten_shared.names import check_env_name
from vecten_shared.output import ExitCode, Failure

# A token this close to expiry is refreshed before the admin call that would carry it.
REFRESH_MARGIN = 30


def session_usable(entry: dict | None, now: float) -> bool:
    """Whether a cache entry can still make an admin call: a token that outlives the refresh
    margin, or a refresh token the next call can spend; a refresh in doubt is neither."""
    if not entry or not entry.get("access_token") or entry.get("refresh_pending"):
        return False
    return entry.get("expires_at", 0) > now + REFRESH_MARGIN or bool(entry.get("refresh_token"))


def login_record(env: str) -> str:
    return f"login/{env}"


class TokenCache:
    def __init__(self, path=None):
        self.path = (
            path
            or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "vecten"
            / "tokens.json"
        )
        self._lock = FileLock(str(self.path) + ".lock", mode=0o600, timeout=60)

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        try:
            with self._lock:
                yield
        except Timeout as exc:
            raise Failure(ExitCode.PREREQUISITE, "authentication cache is busy; retry") from exc

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise Failure(ExitCode.REFUSED, "authentication cache must be a private regular file")
        try:
            return json.loads(self.path.read_text())
        except (ValueError, OSError) as exc:
            raise Failure(
                ExitCode.CONFIG, "authentication cache cannot be read; run vecten logout"
            ) from exc

    def load(self, issuer):
        return self._read().get(issuer)

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".tokens-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def save(self, issuer, tokens):
        with self.locked():
            data = self._read()
            data[issuer] = tokens
            self._write(data)

    def remove(self, issuer):
        with self.locked():
            data = self._read()
            data.pop(issuer, None)
            self._write(data)

    def discard(self, issuer, access_token):
        """Remove the entry only while it still holds `access_token`, so a session another
        process saved since is kept."""
        with self.locked():
            if (self.load(issuer) or {}).get("access_token") == access_token:
                self.remove(issuer)


def endpoint(root, env):
    if env != "local":
        check_env_name(env)
    config = load_config(root)
    if not config.has("mcp"):
        raise Failure(ExitCode.REFUSED, "project has no MCP component")
    if env == "local":
        return "https://mcp.localhost:8443"
    if not config.deploy or env not in config.environments:
        raise Failure(ExitCode.PREREQUISITE, f"environment {env} is not configured")
    return f"https://mcp.{config.deploy.domain}"


def http_client(root=None, env=None, transport=None):
    verify = True
    if root is not None and env == "local":
        cert = root / ".vecten" / "stack" / "certs" / "mcp.localhost.crt"
        if cert.is_file():
            verify = ssl.create_default_context(cafile=str(cert))
    return httpx.Client(timeout=30, verify=verify, follow_redirects=False, transport=transport)


def safe_request(client, method, url, **kwargs):
    try:
        response = client.request(method, url, **kwargs)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise Failure(
            ExitCode.TOOL_FAILED, "MCP request failed; verify the endpoint, TLS and network"
        ) from exc
    if not isinstance(data, dict):
        raise Failure(ExitCode.TOOL_FAILED, "MCP returned an invalid response")
    return response, data


def device_start(issuer, http, cache):
    response, client = safe_request(
        http,
        "POST",
        issuer + "/register",
        json={
            "client_name": "Vecten CLI",
            "redirect_uris": [],
            "token_endpoint_auth_method": "none",
        },
    )
    if response.status_code != 201:
        raise Failure(ExitCode.REFUSED, "MCP refused CLI registration")
    response, device = safe_request(
        http,
        "POST",
        issuer + "/device",
        data={"client_id": client["client_id"], "resource": issuer + "/mcp"},
    )
    if response.status_code != 200:
        raise Failure(ExitCode.REFUSED, "MCP refused device authorization")
    cache.save(
        issuer,
        {
            "pending": device,
            "client_id": client["client_id"],
            "deadline": time.time() + device["expires_in"],
        },
    )
    return {key: value for key, value in device.items() if key != "device_code"}


def device_complete(issuer, http, cache, *, sleep=time.sleep):
    pending = cache.load(issuer)
    if not pending or "pending" not in pending:
        raise Failure(ExitCode.PREREQUISITE, "run vecten login first to request a device code")
    interval = pending["pending"].get("interval", 5)
    while time.time() < pending["deadline"]:
        response, tokens = safe_request(
            http,
            "POST",
            issuer + "/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": pending["client_id"],
                "device_code": pending["pending"]["device_code"],
            },
        )
        if response.status_code == 200:
            cache.save(
                issuer,
                {
                    **tokens,
                    "client_id": pending["client_id"],
                    "expires_at": time.time() + tokens["expires_in"],
                },
            )
            return {"authenticated": True, "issuer": issuer}
        if tokens.get("error") not in {"authorization_pending", "slow_down"}:
            raise Failure(
                ExitCode.REFUSED, "device authorization was refused; run vecten login again"
            )
        if tokens["error"] == "slow_down":
            interval += 5
        sleep(interval)
    raise Failure(ExitCode.REFUSED, "device authorization expired; run vecten login again")


def request_admin(issuer, operation, data=None, *, params=None, cache=None, http=None):
    cache = cache or TokenCache()
    cached = cache.load(issuer)
    if not cached or not cached.get("access_token"):
        raise Failure(ExitCode.PREREQUISITE, "run vecten login before administering the MCP")
    if http is None:
        with http_client() as client:
            return request_admin(issuer, operation, data, params=params, cache=cache, http=client)
    with cache.locked():
        # Another command may have rotated the token while this process waited for the lock.
        cached = cache.load(issuer)
        if not cached or not cached.get("access_token"):
            raise Failure(ExitCode.PREREQUISITE, "run vecten login before administering the MCP")
        if cached.get("refresh_pending"):
            raise Failure(
                ExitCode.PREREQUISITE, "previous token refresh did not finish; run vecten login"
            )
        if cached.get("expires_at", 0) <= time.time() + REFRESH_MARGIN:
            # A lost response or process interruption must never replay a possibly consumed token.
            cache.save(issuer, {**cached, "refresh_pending": True})
            try:
                response, tokens = safe_request(
                    http,
                    "POST",
                    issuer + "/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": cached["client_id"],
                        "refresh_token": cached.get("refresh_token", ""),
                    },
                )
            except Failure as exc:
                raise Failure(
                    ExitCode.PREREQUISITE, "token refresh did not finish; run vecten login"
                ) from exc
            if response.status_code != 200:
                cache.remove(issuer)
                raise Failure(ExitCode.PREREQUISITE, "session expired; run vecten login")
            cached = {
                **tokens,
                "client_id": cached["client_id"],
                "expires_at": time.time() + tokens["expires_in"],
            }
            cache.save(issuer, cached)
    response, result = safe_request(
        http,
        "GET" if data is None else "POST",
        issuer + "/api/admin/" + operation,
        headers={"Authorization": "Bearer " + cached["access_token"]},
        **({"json": data} if data is not None else {"params": params or {}}),
    )
    if response.status_code == 401:
        cache.discard(issuer, cached["access_token"])
        raise Failure(ExitCode.PREREQUISITE, "session is no longer valid; run vecten login")
    if response.status_code == 409 and result.get("data", {}).get("adoption_required"):
        raise Failure(
            ExitCode.REFUSED,
            "warehouse account already exists; review roles and last login, "
            "then repeat with --adopt after any required grant",
            details=result["data"],
        )
    if response.status_code != 200 or not result.get("ok"):
        message = f"MCP refused {operation} (HTTP {response.status_code})"
        reason = result.get("error")
        if isinstance(reason, str) and reason:
            message += ": " + cap(reason, 500)
        raise Failure(ExitCode.REFUSED, message)
    return result["data"]


class AdminClient:
    def __init__(self, root, env):
        self.root, self.env = root, env
        self.issuer = endpoint(root, env)

    def call(self, operation, data=None):
        if not (TokenCache().load(self.issuer) or {}).get("access_token"):
            raise Failure(ExitCode.PREREQUISITE, "run vecten login before administering the MCP")
        with http_client(self.root, self.env) as http:
            return request_admin(self.issuer, operation, data, http=http)

    def read(self, operation, params):
        """A read operation whose arguments travel in the query string."""
        if not (TokenCache().load(self.issuer) or {}).get("access_token"):
            raise Failure(ExitCode.PREREQUISITE, "run vecten login before administering the MCP")
        with http_client(self.root, self.env) as http:
            return request_admin(self.issuer, operation, params=params, http=http)
