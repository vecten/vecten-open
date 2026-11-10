"""The --json and exit code contract every vecten command honours."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    INTERNAL = 1
    USAGE = 2
    PREREQUISITE = 3
    REFUSED = 4
    TOOL_FAILED = 5
    CONFIG = 6


@dataclass
class Result:
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""


class Failure(Exception):
    def __init__(self, code: ExitCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def render(outcome: Result | Failure, as_json: bool) -> tuple[str, int]:
    if isinstance(outcome, Failure):
        if as_json:
            payload = {
                **outcome.details,
                "ok": False,
                "error": outcome.message,
                "code": int(outcome.code),
                "code_name": outcome.code.name,
            }
            return json.dumps(payload, sort_keys=False, default=str), int(outcome.code)
        return f"error: {outcome.message}", int(outcome.code)
    if as_json:
        return json.dumps({**outcome.data, "ok": True}, sort_keys=False, default=str), 0
    return outcome.message, 0
