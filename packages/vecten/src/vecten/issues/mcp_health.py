"""The MCP's own health from the admin API: a semantic model that never became ready, grants
the last reconciliation could not apply or only listed as drift, and access procedures older
than the MCP. Each clears once the MCP reports it fixed."""

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
    if kind != "drift":
        title = f"Grant of {kind} {target} to {group} failed"
    elif failure.get("operation") == "read":
        title = f"Reading the read grants of {group} failed"
    else:
        title = f"Revoking {group}'s drift on {target} failed"
    return Issue(
        id=mcp_issue_id("grant", group, kind, target),
        source="mcp",
        kind="ask",
        title=cap(title, TITLE_LIMIT),
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


def drift_issue(ctx: IssueContext, entry: dict[str, Any], checked_at: Any) -> Issue:
    role, privilege, schema = (str(entry.get(k, "?")) for k in ("role", "privilege", "schema"))
    relation = entry.get("relation")
    place = schema + (f".{relation}" if relation else "")
    parts = [role, privilege, schema, *([str(relation)] if relation else [])]
    return Issue(
        id=mcp_issue_id("drift", *parts),
        source="mcp",
        kind="ask",
        title=cap(
            f"{entry.get('group', '?')}'s role {role} holds {privilege} on {place}", TITLE_LIMIT
        ),
        resolution=(
            "A managed role holds a read grant the MCP's state does not, and reconcile_drift is "
            "report, or the access procedures cannot revoke its kind, so it was listed and kept. "
            "Record it as a grant if it is wanted; otherwise set reconcile_drift to revoke in "
            "the admin panel's settings, and the next reconciliation "
            f"(`vecten reconcile --env {ctx.env}`) revokes what the procedures can."
        ),
        occurrences=1,
        first_seen=None,
        last_seen=iso(checked_at),
        evidence=untrusted(
            {k: sanitized(ctx, entry.get(k)) for k in ("group", "role", "privilege", "schema")}
            | {"relation": sanitized(ctx, relation)}
        ),
    )


def outdated_issue(ctx: IssueContext, outdated: dict[str, Any], checked_at: Any) -> Issue:
    return Issue(
        id=mcp_issue_id("access", "procedures"),
        source="mcp",
        kind="command",
        title="The warehouse's access procedures are older than the MCP",
        resolution=(
            "Area grants wait, and read grants the MCP's state does not hold are neither "
            "listed nor revoked, until a "
            f"warehouse administrator runs the SQL `vecten mcp provisioner-script --env {ctx.env}` "
            "prints, as ACCOUNTADMIN, and the run is confirmed with --confirm-run."
        ),
        occurrences=1,
        first_seen=None,
        last_seen=iso(checked_at),
        evidence=untrusted({k: sanitized(ctx, outdated.get(k)) for k in ("installed", "required")}),
    )


def binding_issue(ctx: IssueContext, entry: dict[str, Any], checked_at: Any) -> Issue:
    group, bound = (str(entry.get(k, "?")) for k in ("group", "bound_role"))
    return Issue(
        id=mcp_issue_id("binding", group, bound),
        source="mcp",
        kind="ask",
        title=cap(f"{group}'s role no longer holds its bound role {bound}", TITLE_LIMIT),
        resolution=(
            "The grant nesting the bound role under the group's role is gone, so the group's "
            "members no longer read through it. To restore it, a warehouse administrator runs "
            f"the statement `vecten group bind {group} {bound} --env {ctx.env}` prints; to end "
            f"the binding, run `vecten group unbind {group} --env {ctx.env}`."
        ),
        occurrences=1,
        first_seen=None,
        last_seen=iso(checked_at),
        evidence=untrusted(
            {k: sanitized(ctx, entry.get(k)) for k in ("group", "role", "bound_role")}
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
    checked_at = access.get("checked_at")
    for failure in access.get("failures") or []:
        if isinstance(failure, dict):
            issues.append(grant_issue(ctx, failure, checked_at))
    for entry in access.get("drift") or []:
        if isinstance(entry, dict):
            issues.append(drift_issue(ctx, entry, checked_at))
    for entry in access.get("missing_bindings") or []:
        if isinstance(entry, dict):
            issues.append(binding_issue(ctx, entry, checked_at))
    if isinstance(access.get("outdated"), dict):
        issues.append(outdated_issue(ctx, access["outdated"], checked_at))
    return issues
