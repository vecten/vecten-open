"""The one factory for boto3 clients in the control plane, and how their errors are reported.

Tests replace `client`.
"""

from __future__ import annotations

from typing import Any

from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.aws import aws_reason

__all__ = ["aws_errors", "aws_failure", "aws_reason", "client", "error_code"]


def client(service: str, region: str) -> Any:
    import boto3

    return boto3.client(service, region_name=region)


def aws_errors() -> tuple[type[Exception], ...]:
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


def aws_failure(operation: str, exc: Exception) -> Failure:
    return Failure(ExitCode.TOOL_FAILED, f"{operation} failed: {aws_reason(exc)}")
