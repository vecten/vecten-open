"""The MCP's own health from the admin API: a semantic model that never became ready, and
grants the last reconciliation could not apply. Both clear once the MCP reports them fixed."""

from __future__ import annotations

from typing import Any

from vecten.issues.context import IssueContext, SourceUnavailable, iso
from vecten.issues.gaps import FIELD_LIMIT, TITLE_LIMIT, sanitized
from vecten_shared.issues import Issue, cap, mcp_issue_id, redact, untrusted

MODEL_RESOLUTION = (
    "The MCP's semantic model is not ready, so every MCP tool but sql_exec fails until it is. "
    "The evidence names what the MCP could not find: usually an osi/ source naming a table the "
    "dbt build does not create, or a build that has not run. Check the osi/ sources against "
    "what `vecten dbt-build-local` builds (the semantic skill does this), fix and ship; the "
    "MCP picks the model up within minutes."
)


def _read(ctx: IssueContext, operation: str) -> dict[str, Any]:
    answer = ctx.admin(operation, {})
    if not isinstance(answer, dict):
        raise SourceUnavailable(f"the admin API returned no {operation} record")
    return answer


def model_issue(ctx: IssueContext, health: dict[str, Any]) -> Issue | None:
    errors = [e for e in health.get("errors") or [] if isinstance(e, dict)]
    if health.get("status") == "ready" or not errors:
        return None
    first = errors[0]
    subject = f" for {first['dataset']}" if first.get("dataset") else ""
    title = f"MCP semantic model not ready: {first.get('code', 'error')}{subject}"
    return Issue(
        id=mcp_issue_id("model"),
        source="mcp",
        kind="ask",
        title=cap(title, TITLE_LIMIT),
        resolution=MODEL_RESOLUTION,
        occurrences=1,
        first_seen=None,
        last_seen=iso(health.get("checked_at")),
        evidence=untrusted(
            {
                "status": sanitized(ctx, health.get("status")),
                "candidate_commit": sanitized(ctx, health.get("candidate_commit")),
                "errors": sanitized(ctx, errors),
            }
        ),
    )


def grant_issue(ctx: IssueContext, failure: dict[str, Any], checked_at: Any) -> Issue:
    group, kind, target = (str(failure.get(k, "?")) for k in ("group", "kind", "target"))
    return Issue(
        id=mcp_issue_id("grant", group, kind, target),
        source="mcp",
        kind="ask",
        title=cap(f"Grant of {kind} {target} to {group} failed", TITLE_LIMIT),
        resolution=(
            f"The warehouse refused to {failure.get('operation', 'apply')} this grant for the "
            "reason in the evidence. A warehouse administrator fixes that cause; then run "
            f"`vecten reconcile --env {ctx.env}` to apply it."
        ),
        occurrences=1,
        first_seen=None,
        last_seen=iso(checked_at),
        evidence=untrusted(
            {
                "operation": sanitized(ctx, failure.get("operation")),
                "error": cap(redact(str(failure.get("error", "")), ctx.secrets), FIELD_LIMIT),
            }
        ),
    )


def collect_mcp_health(ctx: IssueContext) -> list[Issue]:
    if ctx.admin is None:
        raise SourceUnavailable("the admin API is not reachable from here")
    issues = []
    model = model_issue(ctx, _read(ctx, "model-health"))
    if model is not None:
        issues.append(model)
    access = _read(ctx, "access-health")
    for failure in access.get("failures") or []:
        if isinstance(failure, dict):
            issues.append(grant_issue(ctx, failure, access.get("checked_at")))
    return issues
