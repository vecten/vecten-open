"""The issue shape every operations source reports, the ids derived from it, and the rules
that make evidence safe to hand an agent: redacted, capped and marked untrusted.

The DAG alert callback and `vecten issues` both build ids here, so the id a webhook posts is
the id `issues show` takes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

IssueKind = Literal["command", "skill", "ask"]
REDACTED = "[redacted]"
TRUNCATED = "…[truncated]"
# Pointer fields locate the evidence without carrying its text, so the list stays short and
# a skill reads the text itself only through `issues show`.
POINTER_FIELDS = ("run_id", "location", "error_class", "node", "gap_id")
# A known value shorter than this matches ordinary words, so it is left to the patterns.
MIN_SECRET_LENGTH = 6
SYNC_WRAPPER = "ObjectSyncError"

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
_WRAPPED = re.compile(r"^\S+: ([A-Za-z_][A-Za-z0-9_]*): ")
_PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL)
_QUERY = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?]*)\?\S*", re.IGNORECASE)
# Greedy to the last `@` before the path, since a password may itself contain `@`.
_USERINFO = re.compile(r"://[^/\s]*@")
_SECRET_PAIR = re.compile(
    r"\b([\w-]*(?:token|key|password|secret|authorization)[\"']?\s*[:=]\s*[\"']?)"
    r"((?:Basic|Bearer|Token)\s+)?[^\s,;&\"']+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+(?!\[redacted\])\S+", re.IGNORECASE)
_SHAPES = (
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bSCW[0-9A-Z]{17}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
)


@dataclass(frozen=True)
class Issue:
    id: str
    source: str
    kind: IssueKind
    title: str
    resolution: str
    occurrences: int
    first_seen: str | None
    last_seen: str | None
    evidence: dict[str, object] = field(default_factory=lambda: {"untrusted": True})

    def to_json(self, *, full: bool) -> dict:
        evidence = dict(self.evidence)
        if not full:
            kept = ("untrusted", *POINTER_FIELDS)
            evidence = {k: v for k, v in evidence.items() if k in kept}
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "resolution": self.resolution,
            "occurrences": self.occurrences,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "evidence": evidence,
        }


def untrusted(fields: Mapping[str, object]) -> dict[str, object]:
    return {"untrusted": True, **{k: v for k, v in fields.items() if k != "untrusted"}}


def _part(text: str) -> str:
    return _UNSAFE_ID.sub("-", text)


def _id(source: str, *parts: str) -> str:
    return "_".join(("iss", source, *(_part(p) for p in parts)))


def sync_task_key(object_name: str) -> str:
    """The key of one object's mapped sync task: each object fails and recovers on its own."""
    return f"sync.{object_name}"


def airflow_issue_id(dag_id: str, task_key: str, error_class: str) -> str:
    return _id("airflow", dag_id, task_key, error_class)


def dbt_issue_id(name: str) -> str:
    return _id("dbt", name)


def freshness_issue_id(source: str) -> str:
    return _id("dbt", "freshness", source)


def gap_issue_id(gap_id: str) -> str:
    return _id("gap", gap_id.removeprefix("gap-"))


def mcp_issue_id(*parts: str) -> str:
    return _id("mcp", *parts)


def failure_class(exc_type: str, exc_value: str) -> str:
    """The class an Airflow failure is grouped by. The sync task wraps the source's exception
    to name the object, so for that wrapper the class is the one its message names."""
    if exc_type == SYNC_WRAPPER:
        match = _WRAPPED.match(exc_value)
        if match:
            return match.group(1)
    return exc_type


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for value in sorted({s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    text = _PEM.sub(REDACTED, text)
    text = _QUERY.sub(r"\1", text)
    text = _USERINFO.sub(f"://{REDACTED}@", text)
    for shape in _SHAPES:
        text = shape.sub(REDACTED, text)
    text = _SECRET_PAIR.sub(rf"\1\2{REDACTED}", text)
    return _BEARER.sub(f"Bearer {REDACTED}", text)


def cap(text: object, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + TRUNCATED
