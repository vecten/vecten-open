"""Semantic gaps from the admin API. Their state lives in `mcp_state`; a gap leaves `issues`
when the feedback loop resolves it or an admin dismisses it."""

from __future__ import annotations

from vecten.issues.context import IssueContext, SourceUnavailable, iso
from vecten_shared.issues import Issue, cap, gap_issue_id, redact, untrusted

OPEN = ("open", "triaged")
TITLE_LIMIT = 200
FIELD_LIMIT = 2000
# The MCP caps a gap's tool calls at 20; deeper or longer structures are not evidence it sends.
ITEMS = 50
MAX_DEPTH = 4


def _evidence(ctx: IssueContext, gap: dict) -> dict:
    fields: dict[str, object] = {
        "gap_id": gap["id"],
        "status": gap.get("status"),
        "category": gap.get("category"),
        "reason": gap.get("reason"),
        "datasets": gap.get("datasets") or [],
        "columns": gap.get("columns") or [],
        "names": gap.get("names") or [],
        "pull_request": gap.get("pull_request"),
    }
    carried = gap.get("evidence") if isinstance(gap.get("evidence"), dict) else {}
    for name, value in carried.items():
        if name != "untrusted":
            fields[name] = sanitized(ctx, value)
    return untrusted(fields)


def sanitized(ctx: IssueContext, value: object, depth: int = 0) -> object:
    """Every string anywhere in `value` redacted and capped: the MCP nests tool calls as
    `{type, text}` objects whose text is JSON that can hold a credential."""
    if isinstance(value, str):
        return cap(redact(value, ctx.secrets), FIELD_LIMIT)
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, dict):
        return {str(k): sanitized(ctx, v, depth + 1) for k, v in list(value.items())[:ITEMS]}
    if isinstance(value, list):
        return [sanitized(ctx, v, depth + 1) for v in value[:ITEMS]]
    return value if isinstance(value, int | float | bool) or value is None else None


def gap_issue(ctx: IssueContext, gap: dict) -> Issue:
    category = gap.get("category") or "gap"
    reason = gap.get("reason") or ""
    title = cap(f"Semantic gap: {category}" + (f", {reason}" if reason else ""), TITLE_LIMIT)
    return Issue(
        id=gap_issue_id(gap["id"]),
        source="gaps",
        kind="skill",
        title=title,
        resolution="semantic",
        occurrences=int(gap.get("occurrence_count") or 1),
        first_seen=iso(gap.get("first_seen")),
        last_seen=iso(gap.get("last_seen")),
        evidence=_evidence(ctx, gap),
    )


def collect_gaps(ctx: IssueContext) -> list[Issue]:
    if ctx.admin is None:
        raise SourceUnavailable("the admin API is not reachable from here")
    gaps = ctx.admin("gaps", {"status": "all"})
    if not isinstance(gaps, list):
        raise SourceUnavailable("the admin API returned no gap list")
    return [
        gap_issue(ctx, gap)
        for gap in gaps
        if isinstance(gap, dict) and gap.get("id") and gap.get("status", "open") in OPEN
    ]
