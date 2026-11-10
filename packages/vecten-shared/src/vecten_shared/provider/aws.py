"""AWS at run time: secrets from Secrets Manager, connections and logs from the outputs file.

Code on the VMs gets this class. The control-plane half (`bootstrap`, `provision`,
`destroy`, `ship`, and writes to any secret) lives in `vecten.deploy.aws`, which
subclasses it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from vecten_shared.names import check_secret_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import SCOPES, scope_for, secret_id, secret_prefix
from vecten_shared.provider.runtime import RUNTIME_WRITABLE, OutputsRuntimeProvider

__all__ = ["RUNTIME_WRITABLE", "AwsRuntimeProvider", "aws_reason"]

_NOT_FOUND = "ResourceNotFoundException"
_PLAIN_CODE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _aws_errors() -> tuple[type[Exception], ...]:
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def aws_reason(exc: Exception) -> str:
    """The exception type and AWS's error code, as in `ClientError AccessDenied`.

    AWS error messages can quote request parameters, the secret value among them, so the
    message is never kept; an error code is a fixed identifier, kept only when it looks like one.
    """
    code = _error_code(exc)
    if isinstance(code, str) and _PLAIN_CODE.fullmatch(code):
        return f"{type(exc).__name__} {code}"
    return type(exc).__name__


def _store_failure(operation: str, exc: Exception) -> Failure:
    return Failure(ExitCode.TOOL_FAILED, f"Secrets Manager {operation} failed: {aws_reason(exc)}")


class AwsRuntimeProvider(OutputsRuntimeProvider):
    def __init__(self, project_root: Path, env: str):
        super().__init__(project_root, env)
        self._clients: dict[str, Any] = {}

    def _client(self, service: str) -> Any:
        if service not in self._clients:
            import boto3

            self._clients[service] = boto3.client(service, region_name=self.region())
        return self._clients[service]

    def _secret_id(self, name: str) -> str:
        return secret_id(self.config().project.name, self.env, name)

    def get_secret(self, name: str) -> str:
        sid = self._secret_id(name)
        set_hint = f"run `vecten secret set {name} --env {self.env}`"
        client = self._client("secretsmanager")
        try:
            response = client.get_secret_value(SecretId=sid)
        except _aws_errors() as exc:
            if _error_code(exc) == _NOT_FOUND:
                raise Failure(
                    ExitCode.PREREQUISITE, f"secret {name} is not set in {self.env}; {set_hint}"
                ) from None
            raise _store_failure("GetSecretValue", exc) from None
        # Secrets Manager refuses an empty SecretString, so a missing one is a binary secret
        # written outside vecten.
        value = response.get("SecretString")
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
        client = self._client("secretsmanager")
        errors = _aws_errors()
        try:
            client.put_secret_value(SecretId=sid, SecretString=value)
            return
        except errors as exc:
            if _error_code(exc) != _NOT_FOUND:
                raise _store_failure("PutSecretValue", exc) from None
        try:
            client.create_secret(Name=sid, SecretString=value)
        except errors as exc:
            raise _store_failure("CreateSecret", exc) from None

    def list_secrets(self) -> list[str]:
        prefix = secret_prefix(self.config().project.name, self.env)
        client = self._client("secretsmanager")
        names: set[str] = set()
        token: str | None = None
        while True:
            request: dict[str, Any] = {
                "Filters": [{"Key": "name", "Values": [prefix]}],
                "MaxResults": 100,
            }
            if token:
                request["NextToken"] = token
            try:
                response = client.list_secrets(**request)
            except _aws_errors() as exc:
                raise _store_failure("ListSecrets", exc) from None
            for entry in response.get("SecretList", []):
                full = entry.get("Name", "")
                if not full.startswith(prefix):
                    continue
                scope, _, name = full[len(prefix) :].partition("/")
                # get_secret reads a name only under its own scope, so the listing does too.
                if scope in SCOPES and name and scope_for(name) == scope:
                    names.add(name)
            token = response.get("NextToken")
            if not token:
                return sorted(names)
