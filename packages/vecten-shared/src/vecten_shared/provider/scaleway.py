"""Scaleway at run time: secrets from Secret Manager, connections and logs from the outputs file.

Code on the VMs gets this class. It calls Secret Manager's REST API with the VM's scoped key,
`SCW_SECRET_KEY`, as the `X-Auth-Token` header. A logical name `NAME` of scope `s` is the secret
named `NAME` at path `/vecten/<project>/<env>/<s>`, in the environment's resources project:

- the region is the outputs file's `region`;
- the project ID is `[environments.<env>] cloud_ids["project"]` in vecten.toml, which
  `bootstrap` writes; where that is absent, such as a bundle shipped before the fact was
  committed, the `SCW_DEFAULT_PROJECT_ID` the VM agent puts in every container's environment.
  vecten.toml wins because a person's shell often sets `SCW_DEFAULT_PROJECT_ID` to another
  project.

A secret is found by listing with its path and name, then read by ID: IAM's resource-level
conditions on `resource.name` are documented for access by ID, not for access by path.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from typing import Any
from urllib.parse import quote, urlencode

from vecten_shared.names import check_secret_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import SCOPES, scope_for
from vecten_shared.provider.runtime import RUNTIME_WRITABLE, OutputsRuntimeProvider

__all__ = [
    "RUNTIME_WRITABLE",
    "ScalewayRuntimeProvider",
    "SecretManagerError",
    "scaleway_resource_name",
    "scaleway_secret_path",
]

API_ROOT = "https://api.scaleway.com/secret-manager/v1beta1/regions"
SECRET_KEY_VAR = "SCW_SECRET_KEY"
PROJECT_ID_VAR = "SCW_DEFAULT_PROJECT_ID"
PAGE_SIZE = 100
HTTP_TIMEOUT_SECONDS = 30
# Scaleway's own name for "the newest version that is not disabled".
CURRENT_REVISION = "latest_enabled"
_PLAIN_TYPE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def scaleway_secret_path(project: str, env: str, scope: str) -> str:
    return f"/vecten/{project}/{env}/{scope}"


def scaleway_resource_name(project: str, env: str, name: str) -> str:
    """The `resource.name` IAM conditions match: the secret's path, a slash and its name."""
    check_secret_name(name)
    return f"{scaleway_secret_path(project, env, scope_for(name))}/{name}"


def _opener() -> Any:
    """An opener that follows no redirect and verifies against the TLS trust store.

    urllib's redirect handler re-sends every header, `X-Auth-Token` included, to whatever host
    the `Location` names; here a redirect comes back as its own status instead.
    """
    import urllib.request

    from vecten_shared import tls

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=tls.context())
    )


def _open(request: Any, timeout: float) -> Any:
    return _opener().open(request, timeout=timeout)


def _http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    """One HTTPS request; an error status comes back as a status, never as an exception.

    A network or protocol failure names its exception type only, because its text can carry
    the URL or the bytes the server sent.
    """
    import http.client
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _open(request, HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()
    except (OSError, http.client.HTTPException) as exc:
        from vecten_shared import tls

        if tls.certificate_failure(exc):
            raise Failure(
                ExitCode.TOOL_FAILED, f"Secret Manager unreachable: {tls.CERTIFICATE_HINT}"
            ) from None
        raise Failure(
            ExitCode.TOOL_FAILED, f"Secret Manager unreachable: {type(exc).__name__}"
        ) from None


def _error_type(body: bytes) -> str | None:
    try:
        doc = json.loads(body)
    except ValueError, UnicodeDecodeError:
        return None
    kind = doc.get("type") if isinstance(doc, dict) else None
    return kind if isinstance(kind, str) and _PLAIN_TYPE.fullmatch(kind) else None


class SecretManagerError(Exception):
    """An error status from Secret Manager, which `_call` raises and the callers map."""

    def __init__(self, status: int, kind: str | None):
        super().__init__(status)
        self.status = status
        self.kind = kind


def _store_failure(operation: str, error: SecretManagerError) -> Failure:
    """HTTP status and Scaleway's error `type` only: the error message can quote the request."""
    reason = f"HTTP {error.status}" + (f" {error.kind}" if error.kind else "")
    return Failure(ExitCode.TOOL_FAILED, f"Secret Manager {operation} failed: {reason}")


class ScalewayRuntimeProvider(OutputsRuntimeProvider):
    def _project(self) -> str:
        return self.config().project.name

    def _project_id(self) -> str:
        facts = self.config().environments.get(self.env)
        from_config = facts.cloud_ids.get("project") if facts else None
        project_id = from_config or os.environ.get(PROJECT_ID_VAR)
        if not project_id:
            raise Failure(
                ExitCode.CONFIG,
                f'vecten.toml: [environments.{self.env}] cloud_ids["project"] is not set and '
                f"{PROJECT_ID_VAR} is empty; run `vecten bootstrap --env {self.env}`",
            )
        return project_id

    def _secret_key(self) -> str:
        key = os.environ.get(SECRET_KEY_VAR)
        if not key:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"{SECRET_KEY_VAR} is not set; Secret Manager needs the Scaleway API key",
            )
        return key

    def _call(
        self,
        operation: str,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"X-Auth-Token": self._secret_key(), "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        url = f"{API_ROOT}/{quote(self.region(), safe='')}{path}"
        if query:
            url += "?" + urlencode(query)
        status, raw = _http(method, url, headers, body)
        if status >= 400:
            raise SecretManagerError(status, _error_type(raw))
        try:
            doc = json.loads(raw)
        except ValueError, UnicodeDecodeError:
            doc = None
        if not isinstance(doc, dict):
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"Secret Manager {operation} answered HTTP {status} with no JSON object",
            )
        return doc

    def _list(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            request = {
                **query,
                "project_id": self._project_id(),
                "page": page,
                "page_size": PAGE_SIZE,
            }
            try:
                doc = self._call("ListSecrets", "GET", "/secrets", query=request)
            except SecretManagerError as error:
                raise _store_failure("ListSecrets", error) from None
            batch = [s for s in doc.get("secrets") or [] if isinstance(s, dict)]
            found.extend(batch)
            total = doc.get("total_count")
            if not batch or not isinstance(total, int) or len(found) >= total:
                return found
            page += 1

    def _secret_id(self, name: str) -> str | None:
        check_secret_name(name)
        path = scaleway_secret_path(self._project(), self.env, scope_for(name))
        # The name filter is not documented as exact, so the match is made here.
        for entry in self._list({"path": path, "name": name}):
            if entry.get("path") == path and entry.get("name") == name and entry.get("id"):
                return str(entry["id"])
        return None

    def get_secret(self, name: str) -> str:
        set_hint = f"run `vecten secret set {name} --env {self.env}`"
        missing = Failure(
            ExitCode.PREREQUISITE, f"secret {name} is not set in {self.env}; {set_hint}"
        )
        sid = self._secret_id(name)
        if sid is None:
            raise missing
        try:
            doc = self._call(
                "AccessSecretVersion",
                "GET",
                f"/secrets/{quote(sid, safe='')}/versions/{CURRENT_REVISION}/access",
            )
        except SecretManagerError as error:
            # A shell Terraform created with no version yet answers not found.
            if error.status == 404:
                raise missing from None
            raise _store_failure("AccessSecretVersion", error) from None
        try:
            value = base64.b64decode(doc.get("data") or "", validate=True).decode("utf-8")
        except binascii.Error, ValueError, TypeError:
            value = ""
        if not value:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"secret {name} in {self.env} holds no text value; {set_hint}",
            )
        return value

    def put_secret(self, name: str, value: str) -> None:
        check_secret_name(name)
        if not self.may_write(name):
            raise self._write_refused(name)
        sid = self._secret_id(name)
        # Terraform creates every secret a VM writes, and the VM key may only add versions.
        if sid is None:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"secret {name} does not exist in {self.env}; "
                f"run `vecten deploy --env {self.env}` to create it",
            )
        payload = {"data": base64.b64encode(value.encode("utf-8")).decode("ascii")}
        try:
            self._call(
                "CreateSecretVersion",
                "POST",
                f"/secrets/{quote(sid, safe='')}/versions",
                payload=payload,
            )
        except SecretManagerError as error:
            raise _store_failure("CreateSecretVersion", error) from None

    def list_secrets(self) -> list[str]:
        root = scaleway_secret_path(self._project(), self.env, "")
        names: set[str] = set()
        for entry in self._list({}):
            path, name = entry.get("path"), entry.get("name")
            if not isinstance(path, str) or not isinstance(name, str):
                continue
            if not path.startswith(root):
                continue
            scope = path[len(root) :]
            # get_secret reads a name only under its own scope, so the listing does too.
            if scope in SCOPES and name and scope_for(name) == scope:
                names.add(name)
        return sorted(names)
