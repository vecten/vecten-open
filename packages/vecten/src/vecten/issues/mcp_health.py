"""The MCP's own health from the admin API: a semantic model that never became ready, grants
the last reconciliation could not apply, and bound groups an analytics app does not admit. Each
clears once the MCP reports it fixed."""

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


def app_issue(ctx: IssueContext, entry: dict[str, Any], checked_at: Any) -> Issue:
    app, group = (str(entry.get(k, "?")) for k in ("app", "group"))
    where = f"`[apps.{app}]` groups in vecten.toml"
    if entry.get("reason") == "groups differ from the binding":
        name = str(entry.get("warehouse_name", "?"))
        group = name
        groups = ", ".join(str(g) for g in entry.get("groups", [])) or "no group"
        bound = ", ".join(str(g) for g in entry.get("bound", []))
        title = f"App principal {name} is in other groups than {app} is bound to"
        resolution = (
            f"{name} is in {groups}, but vecten.toml binds {app} to {bound}. Run "
            f"`vecten app connect {app} --env {ctx.env} --groups-only` to set its groups to "
            "the binding without changing its credentials."
        )
    elif "principal_id" in entry:
        name = str(entry.get("warehouse_name", "?"))
        group = name
        title = f"Service principal {name} is not marked as {app}'s principal"
        resolution = (
            f"{name} is named as the principal of {app} but the MCP does not hold it to the "
            "app's datasets, so it reads everything its groups can. Review who uses it, then run "
            f"`vecten app connect {app} --env {ctx.env} --adopt` to make it the app's principal "
            "(its groups become the app's and its credentials are replaced), or delete it if "
            "nothing uses it."
        )
    elif "source" in entry:
        dataset, source = str(entry.get("dataset", "?")), str(entry.get("source", "?"))
        title = f"App {app} does not admit group {group}"
        resolution = (
            f"vecten.toml binds {app} to group {group}, but {group} cannot read {source}, which "
            "the app shows, so the gate does not let its members open the app. Grant it with "
            f"`vecten grant {dataset} --group {group} --env {ctx.env}` if they should see that "
            f"data; otherwise remove {group} from the {where} and ship."
        )
    elif "dataset" in entry:
        dataset = str(entry.get("dataset", "?"))
        title = f"App {app} declares dataset {dataset}, which the MCP's model lacks"
        resolution = (
            f"{app} declares the dataset {dataset}, which the MCP's semantic model does not hold, "
            "so no group may open the app. Add the dataset to the osi/ model or remove it from "
            "the app's declaration, then ship."
        )
    else:
        title = f"App {app} is bound to group {group}"
        resolution = (
            f"vecten.toml binds {app} to group {group}, which does not exist on the MCP. Create "
            f"it with `vecten group add {group} --env {ctx.env}`, or remove it from the "
            f"{where} and ship."
        )
    return Issue(
        id=mcp_issue_id("app", app, group),
        source="mcp",
        kind="ask",
        title=cap(title, TITLE_LIMIT),
        resolution=resolution,
        occurrences=1,
        first_seen=None,
        last_seen=iso(checked_at),
        evidence=untrusted(sanitized(ctx, entry)),
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
    for entry in access.get("apps") or []:
        if isinstance(entry, dict):
            issues.append(app_issue(ctx, entry, access.get("checked_at")))
    return issues
