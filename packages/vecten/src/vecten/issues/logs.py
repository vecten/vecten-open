"""The failure in a task log and the lines around it.

Airflow 3 writes task logs as JSON lines, and the line that ends a failed task carries the
exception under `error_detail`, outermost exception first. Anything else is read as plain text
with a Python traceback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

LINES_BEFORE = 12
FRAMES = 8
LINE_LIMIT = 400
_TRACEBACK = "Traceback (most recent call last):"
_EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")


@dataclass(frozen=True)
class LogFailure:
    exc_type: str
    exc_value: str
    excerpt: str


def _clip(line: str) -> str:
    return line if len(line) <= LINE_LIMIT else line[:LINE_LIMIT] + "…"


def _events(text: str) -> list[dict] | None:
    events = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        events.append(event)
    return events


def _render(event: dict) -> str:
    return _clip(f"{event.get('level', '')} {event.get('event', '')}".strip())


def _from_events(events: list[dict]) -> LogFailure | None:
    index = next((i for i in range(len(events) - 1, -1, -1) if events[i].get("error_detail")), None)
    if index is None:
        return None
    detail = events[index]["error_detail"]
    raised = detail[0] if isinstance(detail, list) and detail else {}
    if not isinstance(raised, dict):
        return None
    exc_type = str(raised.get("exc_type") or "Exception")
    exc_value = str(raised.get("exc_value") or "")
    lines = [_render(e) for e in events[max(0, index - LINES_BEFORE) : index + 1]]
    frames = raised.get("frames") if isinstance(raised.get("frames"), list) else []
    for frame in frames[-FRAMES:]:
        if isinstance(frame, dict):
            where = f'  File "{frame.get("filename")}", line {frame.get("lineno")}'
            lines.append(_clip(f"{where}, in {frame.get('name')}"))
    lines.append(_clip(f"{exc_type}: {exc_value}"))
    return LogFailure(exc_type, exc_value, "\n".join(lines))


def _from_text(text: str) -> LogFailure | None:
    lines = text.splitlines()
    start = next((i for i in range(len(lines) - 1, -1, -1) if _TRACEBACK in lines[i]), None)
    if start is None:
        return None
    for end in range(start + 1, len(lines)):
        line = lines[end]
        match = _EXCEPTION_LINE.match(line)
        if match and not line.startswith(" "):
            exc_type = match.group(1).rsplit(".", 1)[-1]
            window = lines[max(0, start - LINES_BEFORE) : end + 1]
            return LogFailure(exc_type, match.group(2) or "", "\n".join(map(_clip, window)))
    return None


def parse_log(text: str) -> LogFailure | None:
    events = _events(text)
    return _from_events(events) if events is not None else _from_text(text)
