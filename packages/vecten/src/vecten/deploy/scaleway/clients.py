"""The one factory for Scaleway REST clients in the control plane, the S3 factory for Object
Storage, and the key they sign with: the human's on the laptop, the deploy key in CI.

Tests replace `client` and `object_storage`. A failure names the HTTP status and Scaleway's
error `type` only: Scaleway's error message can quote the request it refused.
"""

from __future__ import annotations

import json as jsonlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from vecten_shared.output import ExitCode, Failure

__all__ = [
    "ApiError",
    "Credentials",
    "client",
    "credentials",
    "object_storage",
    "s3_endpoint",
    "s3_errors",
    "s3_failure",
    "s3_missing",
    "s3_status",
    "scaleway_failure",
]

API_HOST = "https://api.scaleway.com"
# The versioned root of each API the control plane calls; regional ones take the region and
# zonal ones the zone, both passed as `client`'s `region`.
API_ROOTS = {
    "account": "/account/v3",
    "domain": "/domain/v2beta1",
    "iam": "/iam/v1alpha1",
    "secret-manager": "/secret-manager/v1beta1/regions/{region}",
    "rdb": "/rdb/v1/regions/{region}",
    "instance": "/instance/v1/zones/{region}",
}
ACCESS_KEY_VAR = "SCW_ACCESS_KEY"
SECRET_KEY_VAR = "SCW_SECRET_KEY"
ORGANIZATION_VAR = "SCW_DEFAULT_ORGANIZATION_ID"
CONFIG_PATH_VAR = "SCW_CONFIG_PATH"
PROFILE_VAR = "SCW_PROFILE"
LOGIN_HINT = "run `scw init`, or set SCW_ACCESS_KEY, SCW_SECRET_KEY and SCW_DEFAULT_ORGANIZATION_ID"
HTTP_TIMEOUT_SECONDS = 30
_PLAIN_TYPE = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str = field(repr=False)
    organization_id: str


def _config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_VAR)
    return Path(override) if override else Path.home() / ".config" / "scw" / "config.yaml"


def _from_config() -> dict[str, str]:
    """The scw CLI's settings for the active profile; its values are never echoed."""
    import yaml

    path = _config_path()
    if not path.is_file():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, yaml.YAMLError:
        raise Failure(ExitCode.CONFIG, f"{path} is not a readable scw configuration") from None
    if not isinstance(doc, dict):
        return {}
    found = {k: v for k, v in doc.items() if isinstance(v, str)}
    profile = os.environ.get(PROFILE_VAR) or found.get("active_profile")
    profiles = doc.get("profiles")
    if profile and isinstance(profiles, dict) and isinstance(profiles.get(profile), dict):
        found.update({k: v for k, v in profiles[profile].items() if isinstance(v, str)})
    return found


def credentials(*, organization: bool = True) -> Credentials:
    """The key the control plane signs with: the environment first, then the scw configuration.

    `organization` also requires the organization, which only `bootstrap` needs; CI's deploy
    key arrives as SCW_ACCESS_KEY and SCW_SECRET_KEY alone, and `organization_id` is then
    empty when nothing names one.
    """
    env = {
        "access_key": os.environ.get(ACCESS_KEY_VAR),
        "secret_key": os.environ.get(SECRET_KEY_VAR),
        "default_organization_id": os.environ.get(ORGANIZATION_VAR),
    }
    wanted = ["access_key", "secret_key", *(["default_organization_id"] if organization else [])]
    if not all(env[k] for k in wanted):
        config = _from_config()
        env = {k: v or config.get(k) for k, v in env.items()}
    missing = [k for k in wanted if not env[k]]
    from vecten.deploy.guards import in_actions

    if missing and in_actions():
        # A called workflow reads an environment secret as empty unless its caller passes it.
        raise Failure(
            ExitCode.PREREQUISITE,
            f"no Scaleway credentials ({', '.join(missing)} not set): the job's "
            "VECTEN_SCW_ACCESS_KEY and VECTEN_SCW_SECRET_KEY are empty; set them in the "
            "environment with `vecten bootstrap`, and pass both by name to a called workflow",
        )
    if missing:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"no Scaleway credentials ({', '.join(missing)} not set); {LOGIN_HINT}",
        )
    return Credentials(
        access_key=str(env["access_key"]),
        secret_key=str(env["secret_key"]),
        organization_id=str(env["default_organization_id"] or ""),
    )


class ApiError(Failure):
    """A Scaleway API answer with an error status."""

    def __init__(self, operation: str, status: int, kind: str | None):
        reason = f"HTTP {status}" + (f" {kind}" if kind else "")
        super().__init__(ExitCode.TOOL_FAILED, f"{operation} failed: {reason}")
        self.status = status
        self.kind = kind


def scaleway_failure(operation: str, status: int, kind: str | None) -> ApiError:
    return ApiError(operation, status, kind)


def _opener() -> Any:
    """An opener that follows no redirect.

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
                ExitCode.TOOL_FAILED, f"the Scaleway API is unreachable: {tls.CERTIFICATE_HINT}"
            ) from None
        raise Failure(
            ExitCode.TOOL_FAILED, f"the Scaleway API is unreachable: {type(exc).__name__}"
        ) from None


def _error_type(body: bytes) -> str | None:
    try:
        doc = jsonlib.loads(body)
    except ValueError, UnicodeDecodeError:
        return None
    kind = doc.get("type") if isinstance(doc, dict) else None
    return kind if isinstance(kind, str) and _PLAIN_TYPE.fullmatch(kind) else None


class RestClient:
    """One Scaleway API, signed with the secret key `credentials` found as `X-Auth-Token`."""

    def __init__(self, api: str, region: str | None, creds: Credentials):
        if api not in API_ROOTS:
            raise Failure(ExitCode.INTERNAL, f"no Scaleway API named {api!r}")
        root = API_ROOTS[api]
        if "{region}" in root:
            if not region:
                raise Failure(ExitCode.INTERNAL, f"the Scaleway {api} API is regional")
            root = root.format(region=quote(region, safe=""))
        self.api = api
        self._base = f"{API_HOST}{root}"
        self._creds = creds

    def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        data: str | None = None,
    ) -> dict[str, Any]:
        """One call; `json` goes as a JSON body, `data` as a plain-text one (Instances user
        data takes its value that way)."""
        headers = {"X-Auth-Token": self._creds.secret_key, "Accept": "application/json"}
        body = None
        if json is not None:
            headers["Content-Type"] = "application/json"
            body = jsonlib.dumps(json).encode("utf-8")
        elif data is not None:
            headers["Content-Type"] = "text/plain"
            body = data.encode("utf-8")
        url = f"{self._base}{path}"
        # Scaleway reads booleans in a query as `true`/`false`, not Python's spelling.
        query = {
            k: (str(v).lower() if isinstance(v, bool) else v)
            for k, v in (params or {}).items()
            if v is not None
        }
        if query:
            url += "?" + urlencode(query, doseq=True)
        operation = f"Scaleway {self.api} {method} {path}"
        status, raw = _http(method, url, headers, body)
        if status >= 400:
            raise ApiError(operation, status, _error_type(raw))
        if not raw.strip():
            return {}
        try:
            doc = jsonlib.loads(raw)
        except ValueError, UnicodeDecodeError:
            doc = None
        if not isinstance(doc, dict):
            raise Failure(
                ExitCode.TOOL_FAILED, f"{operation} answered HTTP {status} with no JSON object"
            )
        return doc


def client(api: str, region: str | None = None) -> RestClient:
    return RestClient(api, region, credentials(organization=False))


def s3_endpoint(region: str) -> str:
    return f"https://s3.{region}.scw.cloud"


def s3_errors() -> tuple[type[Exception], ...]:
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def s3_status(exc: Exception) -> tuple[str | None, int | None]:
    """The S3 error code and HTTP status of a boto3 error; both None for a client-side one."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None, None
    code = response.get("Error", {}).get("Code")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code, status


def s3_missing(exc: Exception) -> bool:
    """Whether a boto3 error says the object or bucket is not there."""
    code, status = s3_status(exc)
    return status == 404 or code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound")


def s3_failure(operation: str, exc: Exception) -> Failure:
    """The S3 error code or the exception type only: an error message can quote the request."""
    code = s3_status(exc)[0]
    return Failure(ExitCode.TOOL_FAILED, f"S3 {operation} failed: {code or type(exc).__name__}")


def object_storage(region: str, access_key: str, secret_key: str, project_id: str) -> Any:
    """A boto3 S3 client on Scaleway Object Storage, acting in `project_id`.

    A key's buckets are those of its preferred project; `<access key>@<project ID>` makes the
    S3 API act in another project the key holds Object Storage rights on.
    """
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=s3_endpoint(region),
        aws_access_key_id=f"{access_key}@{project_id}",
        aws_secret_access_key=secret_key,
        config=Config(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
