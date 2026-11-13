"""The MCP's own health from the admin API: a semantic model that never became ready, grants
the last reconciliation could not apply or only listed as drift, access procedures older than
the MCP, and bound groups an analytics app does not admit. Each clears once the MCP reports it
fixed."""

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


def app_issue(ctx: IssueContext, entry: dict[str, Any], checked_at: Any) -> Issue:
    app, group = (str(entry.get(k, "?")) for k in ("app", "group"))
    where = f"`[apps.{app}]` groups in vecten.toml"
    reason = entry.get("reason")
    roles = ", ".join(str(r) for r in entry.get("roles", [])) or "?"
    if reason == "role sync failed":
        name = str(entry.get("warehouse_name", "?"))
        group = f"{name} roles"
        title = f"The MCP could not set the warehouse roles of {name}"
        resolution = (
            f"{name}, the principal of {app}, may hold some of {roles}: setting its roles to "
            f"those of the groups {app} admits failed ({entry.get('error', 'unknown error')}). "
            "Until it succeeds its queries may miss an admitted group's data, and are refused "
            "on Postgres while a role to revoke remains. The MCP retries at every "
            f"reconciliation; `vecten reconcile --env {ctx.env}` retries now."
        )
    elif reason == "warehouse roles beyond the admitted groups":
        name = str(entry.get("warehouse_name", "?"))
        group = f"{name} extra roles"
        title = f"App principal {name} holds warehouse roles beyond its admitted groups"
        resolution = (
            f"{name}, the principal of {app}, holds {roles} in the warehouse, beyond the roles "
            f"of the groups {app} admits, so its queries are refused on Postgres. A warehouse "
            f"administrator revokes them (REVOKE <role> FROM {name}); a role the MCP granted is "
            f"revoked again by `vecten reconcile --env {ctx.env}`."
        )
    elif reason == "groups differ from the binding":
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


def nested_role_issue(ctx: IssueContext, entry: dict[str, Any], checked_at: Any) -> Issue:
    group, granted = (str(entry.get(k, "?")) for k in ("group", "granted_role"))
    return Issue(
        id=mcp_issue_id("nested", group, granted),
        source="mcp",
        kind="ask",
        title=cap(f"{group}'s role holds {granted}, which no confirmed binding names", TITLE_LIMIT),
        resolution=(
            f"Every member of {group} reads through {granted}. If it should, bind and confirm "
            f"it (`vecten group bind {group} {granted} --env {ctx.env}`, then `--confirm`); "
            "otherwise a warehouse administrator REVOKEs it from the group's role. The MCP "
            "never revokes a role it does not manage."
        ),
        occurrences=1,
        first_seen=None,
        last_seen=iso(checked_at),
        evidence=untrusted(
            {k: sanitized(ctx, entry.get(k)) for k in ("group", "role", "granted_role")}
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
    for entry in access.get("unbound_roles") or []:
        if isinstance(entry, dict):
            issues.append(nested_role_issue(ctx, entry, checked_at))
    if isinstance(access.get("outdated"), dict):
        issues.append(outdated_issue(ctx, access["outdated"], checked_at))
    for entry in access.get("apps") or []:
        if isinstance(entry, dict):
            issues.append(app_issue(ctx, entry, checked_at))
    return issues
