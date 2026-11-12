"""One function per panel screen; every read and write goes through the admin service."""

import json
from datetime import UTC, date, datetime
from urllib.parse import urlencode

from vecten_mcp.admin_service import SETTINGS
from vecten_mcp.administration import AdoptionRequired
from vecten_mcp.analytics import DEFAULT_DAYS
from vecten_mcp.panel.html import done, field, form, hidden, join, layout, link, table, tag
from vecten_mcp.tool_service import STATUSES

DONE = {
    "users/create": "Person created.",
    "users/set-admin": "Admin flag saved.",
    "users/disable": "Principal disabled.",
    "users/delete": "Principal deleted.",
    "groups/create": "Group created.",
    "groups/delete": "Group deleted.",
    "groups/member": "Membership saved.",
    "groups/confirm": "Binding confirmed.",
    "grant": "Grant saved.",
    "revoke": "Grant revoked.",
    "revoke-refused": (
        "Revoke recorded, but the warehouse refused the revoke; every reconciliation retries "
        "it, and access health shows why."
    ),
    "denied/set": "Deny list saved.",
    "reconcile": "Grants reconciled.",
    "gaps/status": "Gap status saved.",
    "settings/set": "Setting saved.",
}
TOOLS = ("query_metrics", "sql-exec", "describe", "search", "report_feedback", "verify_receipt")
RESOLUTIONS = ("compiled", "agent_sql", "refused", "error")


def when(at):
    if not isinstance(at, (int, float)):
        return ""
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M UTC")


def notice(ctx):
    return DONE.get(ctx.query("done", ""))


def page(ctx, title, *parts, status=200):
    return layout(title, join(*parts), csrf=ctx.csrf, status=status, notice=notice(ctx))


def h2(text):
    return tag("h2", text)


def hint(text):
    return tag("p", text, class_="hint")


def pre(value):
    if not isinstance(value, str):
        value = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return tag("pre", value)


def select(name, options, selected=None, *, label=None, blank=None):
    choices = [tag("option", blank, value="")] if blank is not None else []
    for option in options:
        value, text = option if isinstance(option, tuple) else (option, option)
        choices.append(tag("option", text, value=value, selected=value == selected))
    control = tag("select", join(*choices), name=name)
    return tag("label", join(label, " ", control)) if label else control


def flag(value):
    return "true" if value else "false"


def home(ctx):
    health = ctx.call("model-health")
    usage = ctx.call("usage", {"days": 7})
    rows = [
        ("Model", health.get("status")),
        ("Live commit", health.get("live_commit") or "none"),
        ("Questions, last 7 days", usage["questions"]),
        ("Refused, last 7 days", sum(usage["refusals"].values())),
        ("Open or triaged gaps", usage["open_gaps"]),
    ]
    return page(ctx, "Overview", table(("", "Value"), rows))


def users(ctx):
    people = ctx.call("users", {"kind": "all"})
    groups = ctx.call("groups")
    members = ctx.call("memberships")
    names = {p["id"]: p["name"] for p in people}
    humans = [p for p in people if p["kind"] == "human"]
    services = [p for p in people if p["kind"] == "service"]

    def actions(principal):
        parts = []
        if principal["kind"] == "human":
            parts.append(
                form(
                    f"/admin/users/{principal['id']}/admin",
                    ctx.csrf,
                    hidden("admin", flag(not principal["admin"])),
                    button="Clear admin" if principal["admin"] else "Make admin",
                    class_="inline",
                )
            )
        if principal["enabled"]:
            parts.append(
                form(
                    f"/admin/users/{principal['id']}/disable",
                    ctx.csrf,
                    button="Disable",
                    class_="inline",
                )
            )
        parts.append(
            form(
                f"/admin/users/{principal['id']}/delete", ctx.csrf, button="Delete", class_="inline"
            )
        )
        return join(*parts)

    def state(principal):
        if not principal["provisioned"]:
            return "invited"
        return ("adopted" if principal["adopted"] else "provisioned") + (
            "" if principal["enabled"] else ", disabled"
        )

    people_table = table(
        ("Name", "Email", "Warehouse user", "Admin", "State", ""),
        [
            (p["name"], p["email"], p["warehouse_name"], p["admin"], state(p), actions(p))
            for p in humans
        ],
    )
    create_person = form(
        "/admin/users/create",
        ctx.csrf,
        field("Email", "email", type_="email"),
        field("Display name", "name"),
        field("Warehouse user (optional)", "warehouse_name", required=False),
        button="Create and provision",
    )
    services_table = table(
        ("Name", "Warehouse user", "State", ""),
        [(p["name"], p["warehouse_name"], state(p), actions(p)) for p in services],
        empty="No service principals.",
    )
    create_service = form(
        "/admin/services/create",
        ctx.csrf,
        field("Service name", "name", pattern="[a-z][a-z0-9_]*"),
        button="Create service principal",
    )
    grouped = {}
    for row in members:
        grouped.setdefault(row["group_name"], []).append(names.get(row["principal_id"], "?"))

    def binding(g):
        if "bound_role" not in g:
            return ""
        actions = [
            form(f"/admin/groups/{g['name']}/unbind", ctx.csrf, button="Unbind", class_="inline")
        ]
        if g["binding"] == "pending":
            actions.insert(
                0,
                form(
                    f"/admin/groups/{g['name']}/confirm",
                    ctx.csrf,
                    button="Confirm",
                    class_="inline",
                ),
            )
        return join(f"{g['bound_role']} ({g['binding']})", *actions)

    groups_table = table(
        ("Group", "Warehouse role", "Bound role", "Members", ""),
        [
            (
                g["name"],
                g["warehouse_role"],
                binding(g),
                ", ".join(sorted(grouped.get(g["name"], []))),
                ""
                if g["name"] == "default"
                else form(
                    f"/admin/groups/{g['name']}/delete", ctx.csrf, button="Delete", class_="inline"
                ),
            )
            for g in groups
        ],
    )
    provisioned = [
        (p["id"], f"{p['name']} ({p['warehouse_name']})") for p in people if p["provisioned"]
    ]
    membership = form(
        "/admin/groups/member",
        ctx.csrf,
        select("group", [g["name"] for g in groups], label="Group"),
        select("id", provisioned, label="Principal"),
        select("member", [("true", "Add"), ("false", "Remove")], label="Change"),
        button="Save membership",
    )
    create_group = form(
        "/admin/groups/create",
        ctx.csrf,
        field("Group name", "name", pattern="[a-z][a-z0-9_]*"),
        field("Warehouse role (the group name if empty)", "role", required=False),
        button="Create group",
    )
    bind_role = form(
        "/admin/groups/bind",
        ctx.csrf,
        select("group", [g["name"] for g in groups if g["name"] != "default"], label="Group"),
        field("Existing warehouse role", "role"),
        button="Bind role",
    )
    return page(
        ctx,
        "Users and groups",
        h2("People"),
        people_table,
        h2("Create a person"),
        hint(
            "The warehouse user is derived from the display name. If an account of that name "
            "already exists you are asked to confirm adoption before anything changes."
        ),
        create_person,
        h2("Service principals"),
        services_table,
        create_service,
        h2("Groups"),
        groups_table,
        create_group,
        membership,
        h2("Bind an existing role"),
        hint(
            "The group's members read through a role the warehouse already has. The server "
            "prints the statement; an administrator runs it, then confirms here. The server "
            "never grants, alters or drops the bound role."
        ),
        bind_role,
    )


def adoption(ctx, exc, action, fields):
    account = exc.account
    rows = [
        ("Account", account.name),
        ("Roles", ", ".join(account.roles) or "none"),
        ("Last login", account.last_login or "unknown"),
    ]
    parts = [
        tag(
            "p",
            "A warehouse account of this name already exists. Nothing has changed. Review it, "
            "then adopt it to bind it to this principal without touching its credentials.",
        ),
        table(("", ""), rows),
    ]
    if account.adoption_sql:
        parts += [
            tag("p", "An administrator must run this statement in the warehouse first:"),
            pre(account.adoption_sql),
        ]
    parts.append(
        form(
            action,
            ctx.csrf,
            *(hidden(name, value) for name, value in fields.items() if value),
            hidden("adopt", "true"),
            button="Adopt existing account",
        )
    )
    parts.append(link("/admin/users", "Cancel"))
    return page(ctx, "Account already exists", *parts)


def users_create(ctx):
    data = {
        "email": ctx.form.get("email", ""),
        "name": ctx.form.get("name", ""),
        "warehouse_name": ctx.form.get("warehouse_name") or None,
        "adopt": ctx.form.get("adopt") == "true",
    }
    try:
        ctx.call("users/create", data, write=True)
    except AdoptionRequired as exc:
        fields = {k: data[k] for k in ("email", "name", "warehouse_name")}
        return adoption(ctx, exc, "/admin/users/create", fields)
    return done("/admin/users", "users/create")


def services_create(ctx):
    data = {"name": ctx.form.get("name", ""), "adopt": ctx.form.get("adopt") == "true"}
    try:
        created = ctx.call("services/create", data, write=True)
    except AdoptionRequired as exc:
        return adoption(ctx, exc, "/admin/services/create", {"name": data["name"]})
    credentials = created["credentials"]
    rows = [
        ("Warehouse user", created["warehouse_name"]),
        ("Client id", credentials["client_id"]),
        ("Client secret", credentials["client_secret"]),
    ]
    return page(
        ctx,
        "Service principal created",
        tag("p", "Copy the secret now. It is shown once and cannot be read again."),
        table(("", ""), rows),
        link("/admin/users", "Back to users"),
    )


def principal_action(operation):
    def handler(ctx):
        data = {"id": ctx.param("principal_id")}
        if operation == "users/set-admin":
            data["admin"] = ctx.form.get("admin") == "true"
        ctx.call(operation, data, write=True)
        return done("/admin/users", operation)

    return handler


def groups_create(ctx):
    data = {"name": ctx.form.get("name", ""), "role": ctx.form.get("role", "") or None}
    ctx.call("groups/create", data, write=True)
    return done("/admin/users", "groups/create")


def groups_bind(ctx):
    data = {"group": ctx.form.get("group", ""), "role": ctx.form.get("role", "")}
    bound = ctx.call("groups/bind", data, write=True)
    return page(
        ctx,
        "Bind an existing role",
        tag("p", f"Review what {bound['bound_role']} reads, as an administrator:"),
        pre(bound["review"]),
        tag("p", "Then an administrator runs this statement in the warehouse:"),
        pre(bound["statement"]),
        form(f"/admin/groups/{bound['group']}/confirm", ctx.csrf, button="Confirm binding"),
        link("/admin/users", "Back"),
    )


def groups_confirm(ctx):
    ctx.call("groups/confirm", {"group": ctx.param("group")}, write=True)
    return done("/admin/users", "groups/confirm")


def groups_unbind(ctx):
    unbound = ctx.call("groups/unbind", {"group": ctx.param("group")}, write=True)
    return page(
        ctx,
        "Binding forgotten",
        tag("p", "An administrator runs this statement to remove the grant:"),
        pre(unbound["statement"]),
        link("/admin/users", "Back"),
    )


def groups_delete(ctx):
    ctx.call("groups/delete", {"name": ctx.param("group")}, write=True)
    return done("/admin/users", "groups/delete")


def groups_member(ctx):
    data = {
        "group": ctx.form.get("group", ""),
        "id": ctx.form.get("id", ""),
        "member": ctx.form.get("member") == "true",
    }
    ctx.call("groups/member", data, write=True)
    return done("/admin/users", "groups/member")


def access(ctx):
    grants = ctx.call("grants")
    denied = {row["group"]: row["columns"] for row in ctx.call("denied")}
    groups = [g["name"] for g in ctx.call("groups")]

    def revoke(grant):
        return form(
            "/admin/access/grant",
            ctx.csrf,
            hidden("group", grant["group"]),
            hidden("kind", grant["kind"]),
            hidden("target", grant["target"]),
            hidden("action", "revoke"),
            button="Revoke",
            class_="inline",
        )

    grants_table = table(
        ("Group", "Kind", "Target", ""),
        [
            (g["group"], g["kind"], g["target"], revoke(g))
            for g in sorted(grants, key=lambda g: (g["group"], g["kind"], g["target"]))
        ],
        empty="No grants.",
    )
    grant_form = form(
        "/admin/access/grant",
        ctx.csrf,
        select("group", groups, label="Group"),
        select("kind", ["dataset", "schema", "area"], label="Kind"),
        field("Dataset, schema or area", "target"),
        hidden("action", "grant"),
        button="Grant",
    )
    deny_forms = join(
        *(
            form(
                "/admin/access/denied",
                ctx.csrf,
                hidden("group", group),
                tag(
                    "label",
                    join(
                        f"Deny list for {group} (one dataset.field per line)",
                        tag("textarea", "\n".join(denied.get(group, [])), name="columns"),
                    ),
                ),
                button=f"Save {group}",
            )
            for group in groups
        )
    )
    return page(
        ctx,
        "Grants and deny lists",
        h2("Grants"),
        hint(
            "Grants are reconciled every fifteen minutes, on model change and on demand. "
            "An area grant covers both of its schemas, core_<area> and marts_<area>."
        ),
        grants_table,
        grant_form,
        form("/admin/access/reconcile", ctx.csrf, button="Reconcile now"),
        h2("Deny list"),
        hint(
            "Denied fields disappear from discovery and compiled queries for the group. "
            "This hides definitions; warehouse permissions enforce access to data."
        ),
        deny_forms,
    )


def access_grant(ctx):
    operation = "revoke" if ctx.form.get("action") == "revoke" else "grant"
    data = {k: ctx.form.get(k, "") for k in ("group", "kind", "target")}
    result = ctx.call(operation, data, write=True) or {}
    failures = (result.get("reconciliation") or {}).get("failures", [])
    mine = (data["group"], data["kind"], data["target"], "revoke")
    if any((f["group"], f["kind"], f["target"], f["operation"]) == mine for f in failures):
        return done("/admin/access", "revoke-refused")
    return done("/admin/access", operation)


def access_denied(ctx):
    columns = [line.strip() for line in ctx.form.get("columns", "").splitlines() if line.strip()]
    ctx.call("denied/set", {"group": ctx.form.get("group", ""), "columns": columns}, write=True)
    return done("/admin/access", "denied/set")


def access_reconcile(ctx):
    ctx.call("reconcile", {}, write=True)
    return done("/admin/access", "reconcile")


def health_tables(health):
    rows = [
        ("Status", health.get("status")),
        ("Live commit", health.get("live_commit") or "none"),
        ("Candidate commit", health.get("candidate_commit") or "none"),
        ("Checked", when(health.get("checked_at"))),
    ]
    issues = [
        (kind, issue.get("code"), issue.get("dataset") or "", issue.get("message") or "")
        for kind in ("errors", "warnings")
        for issue in health.get(kind, [])
    ]
    return join(
        table(("", ""), rows),
        table(("", "Code", "Dataset", "Message"), issues, empty="No errors or warnings."),
    )


def model(ctx):
    data = ctx.call("model", {"days": days(ctx)})
    gaps = ctx.call("analytics", {"view": "open_gaps"})["gaps"]
    parts = [
        hint(
            "The panel shows definitions; it does not edit them. Change a definition with the "
            f"`{data['semantic_skill']}` skill in a checkout of the project repository."
        ),
        h2("Health"),
        health_tables(data["health"]),
    ]
    for dataset in data["datasets"]:
        parts += [
            h2(f"Dataset {dataset['name']}"),
            tag("p", dataset["description"] or "No description.", class_="hint"),
            table(
                ("Field", "Kind", "Description", f"Uses, {data['usage_days']} days"),
                [
                    (
                        link(
                            "/admin/model/field?"
                            + urlencode({"dataset": dataset["name"], "field": f["name"]}),
                            f["name"],
                        ),
                        f["classification"],
                        f["description"] or "no description",
                        f["count"],
                    )
                    for f in dataset["fields"]
                ],
            ),
        ]
    parts += [
        h2("Metrics"),
        table(
            ("Metric", "Dataset", "Description", f"Uses, {data['usage_days']} days", "Defined in"),
            [
                (m["name"], m["dataset"], m["description"], m["count"], m["described_in"])
                for m in data["metrics"]
            ],
        ),
        h2("Open gaps"),
        gaps_table(gaps),
    ]
    return page(ctx, "Model", *parts)


def model_field(ctx):
    data = ctx.call("model", {"days": days(ctx)})
    dataset, name = ctx.query("dataset", ""), ctx.query("field", "")
    found = next(
        (
            f
            for d in data["datasets"]
            if d["name"] == dataset
            for f in d["fields"]
            if f["name"] == name
        ),
        None,
    )
    if found is None:
        raise ValueError("unknown dataset field")
    rows = [
        ("Dataset", dataset),
        ("Kind", found["classification"]),
        ("Description", found["description"] or "no description"),
        ("Description lives in", found["described_in"]),
        (f"Uses, {data['usage_days']} days", found["count"]),
    ]
    return page(
        ctx,
        f"Field {dataset}.{name}",
        table(("", ""), rows),
        tag(
            "p",
            f"To change this description, run the `{data['semantic_skill']}` skill in a checkout "
            "of the project repository; it opens a pull request. The panel offers no edit.",
        ),
        link("/admin/model", "Back to the model"),
    )


def days(ctx):
    value = ctx.query("days")
    if value is None or value == "":
        return None
    if not value.isdecimal():
        raise ValueError("days must be a whole number")
    return int(value)


def gaps_table(gaps):
    return table(
        ("Gap", "Category", "Reason", "Occurrences", "Last seen", "Status", "Pull request"),
        [
            (
                link(f"/admin/gaps/{g['id']}", g["id"]),
                g.get("category"),
                g.get("reason"),
                g.get("occurrence_count"),
                when(g.get("last_seen")),
                g.get("status"),
                g.get("pull_request") or "",
            )
            for g in gaps
        ],
        empty="No gaps.",
    )


def analytics(ctx):
    window = days(ctx) or DEFAULT_DAYS

    def view(name, **extra):
        return ctx.call("analytics", {"view": name, "days": extra.get("days", window)})

    asked = view("questions")
    top = view("top")
    unused = ctx.call("analytics", {"view": "unused_fields"})
    undescribed = view("undescribed_fields")
    refusals = view("refusals")
    gaps = view("open_gaps")
    picker = form(
        "/admin/analytics",
        None,
        field("Days", "days", value=window, type_="number", min="1", max="3650"),
        button="Show",
        method="get",
    )
    return page(
        ctx,
        "Usage",
        picker,
        h2(f"Questions per day, last {window} days ({asked['total']} in total)"),
        table(("Day", "Questions"), [(r["day"], r["questions"]) for r in asked["by_day"]]),
        h2("Questions per person and day"),
        table(
            ("Day", "Person", "Questions"),
            [(r["day"], r["name"], r["questions"]) for r in asked["rows"]],
        ),
        h2("Top metrics"),
        table(("Metric", "Answers"), [(m["name"], m["count"]) for m in top["metrics"]]),
        h2("Top datasets"),
        table(("Dataset", "Queries"), [(d["name"], d["count"]) for d in top["datasets"]]),
        h2(f"Fields never used, last {unused['days']} days"),
        table(("Dataset", "Field"), [(f["dataset"], f["field"]) for f in unused["fields"]]),
        h2("Fields with no description"),
        table(("Dataset", "Field"), [(f["dataset"], f["field"]) for f in undescribed["fields"]]),
        h2("Refusal reasons over time"),
        table(
            ("Day", "Reason", "Refusals"),
            [(r["day"], r["reason"], r["count"]) for r in refusals["by_day"]],
        ),
        h2("Recent refusals"),
        table(
            ("Time", "Person", "Reason", "Question"),
            [
                (
                    link(f"/admin/audit/{r['id']}", when(r["at"])),
                    r["name"],
                    r["reason"],
                    r["question"],
                )
                for r in refusals["recent"]
            ],
        ),
        h2("Open gaps"),
        gaps_table(gaps["gaps"]),
    )


def gaps(ctx):
    status = ctx.query("status", "open")
    listed = ctx.call("gaps", {"status": status})
    picker = form(
        "/admin/gaps",
        None,
        select("status", [*STATUSES, "all"], status, label="Status"),
        button="Show",
        method="get",
    )
    return page(ctx, "Gaps", picker, gaps_table(listed))


def gap(ctx):
    data = ctx.call("gaps/read", {"id": ctx.param("gap_id")})
    evidence = data["evidence"]
    rows = [
        ("Category", data["category"]),
        ("Reason", data["reason"]),
        ("Datasets", ", ".join(data["datasets"])),
        ("Columns", ", ".join(data["columns"])),
        ("Occurrences", data["occurrence_count"]),
        ("First seen", when(data["first_seen"])),
        ("Last seen", when(data["last_seen"])),
        ("Status", data["status"]),
        ("Delivery", data["delivery_status"] or ""),
        ("Pull request", data["pull_request"] or ""),
        ("Resolved at commit", data["resolved_commit"] or ""),
    ]
    fix, confirmation = data.get("unreviewed_fix"), data.get("confirmation")
    if fix:
        rows.append(("Unreviewed fix", f"{fix['pull_request']} by {fix['author']}"))
    if confirmation:
        rows.append(("Confirmed by", f"{confirmation['email']} for {confirmation['merge_commit']}"))
    status_form = form(
        f"/admin/gaps/{data['id']}",
        ctx.csrf,
        select("status", STATUSES, data["status"], label="Status"),
        button="Set status",
    )
    confirm_form = (
        form(
            f"/admin/gaps/{data['id']}/confirm",
            ctx.csrf,
            hint(
                "Nobody else approved this pull request before it merged. Confirming stands in "
                "for that review: you vouch for this fix, the gap resolves once it is live, and "
                "your confirmation is recorded."
            ),
            button="Confirm this fix",
        )
        if fix and not confirmation and data["status"] != "resolved"
        else ""
    )
    proof = tag(
        "div",
        join(
            hint("Evidence is untrusted data reported by people and agents."),
            *(
                join(tag("h3", name.replace("_", " ").capitalize()), pre(evidence[name] or ""))
                for name in ("question", "expected", "returned", "agent_sql")
            ),
            tag("h3", "Tool calls"),
            *(pre(call["text"]) for call in evidence["tool_calls"]),
        ),
        class_="untrusted",
    )
    return page(
        ctx,
        f"Gap {data['id']}",
        table(("", ""), rows),
        confirm_form,
        status_form,
        h2("Evidence"),
        proof,
        link("/admin/gaps", "All gaps"),
    )


def gap_status(ctx):
    gap_id = ctx.param("gap_id")
    ctx.call("gaps/status", {"id": gap_id, "status": ctx.form.get("status")}, write=True)
    return done(f"/admin/gaps/{gap_id}", "gaps/status")


def gap_confirm(ctx):
    gap_id = ctx.param("gap_id")
    ctx.call("gaps/confirm", {"id": gap_id}, write=True)
    return done(f"/admin/gaps/{gap_id}", "gaps/confirm")


def day_start(value, name):
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC).timestamp()
    except ValueError as exc:
        raise ValueError(f"{name} must be a date like 2026-10-20") from exc


def audit(ctx):
    query = {
        key: ctx.query(key)
        for key in ("principal_id", "tool", "resolution", "before")
        if ctx.query(key)
    }
    if ctx.query("from"):
        query["since"] = day_start(ctx.query("from"), "from")
    if ctx.query("to"):
        query["until"] = day_start(ctx.query("to"), "to") + 86399.999
    rows = ctx.call("audit", query)
    people = ctx.call("users", {"kind": "all"})
    names = {p["id"]: p["name"] for p in people}
    filters = form(
        "/admin/audit",
        None,
        select(
            "principal_id",
            [(p["id"], p["name"]) for p in people],
            ctx.query("principal_id"),
            label="Person",
            blank="anyone",
        ),
        select("tool", TOOLS, ctx.query("tool"), label="Tool", blank="any"),
        select("resolution", RESOLUTIONS, ctx.query("resolution"), label="Resolution", blank="any"),
        field("From", "from", value=ctx.query("from", ""), type_="date", required=False),
        field("To", "to", value=ctx.query("to", ""), type_="date", required=False),
        button="Filter",
        method="get",
    )
    listing = table(
        ("Time", "Receipt", "Person", "Tool", "Resolution", "Reason", "Question"),
        [
            (
                when(r.get("at")),
                link(f"/admin/audit/{r['id']}", r["id"]),
                names.get(r.get("principal_id"), r.get("principal_id")),
                r.get("tool"),
                r.get("resolution"),
                r.get("refusal_reason") or "",
                str(r.get("question") or "")[:160],
            )
            for r in rows
        ],
        empty="No audit rows match.",
    )
    parts = [filters, listing]
    if len(rows) >= 50:
        older = {k: v for k, v in ctx.request.query_params.items() if k != "before"}
        older["before"] = rows[-1]["at"]
        parts.append(link("/admin/audit?" + urlencode(older), "Older rows"))
    return page(ctx, "Audit", *parts)


def audit_row(ctx):
    data = ctx.call("audit/read", {"id": ctx.param("row_id")})
    row = data["row"]
    rows = [
        ("Receipt", row.get("id")),
        ("Time", when(row.get("at"))),
        ("Person", row.get("principal_id")),
        ("Client", row.get("client_id")),
        ("Tool", row.get("tool")),
        ("Resolution", row.get("resolution")),
        ("Refusal reason", row.get("refusal_reason") or ""),
        ("Error class", row.get("error_class") or ""),
        ("Model commit", row.get("model_commit") or ""),
        ("Rows", row.get("row_count")),
        ("Duration, ms", row.get("duration_ms")),
        ("Plugin", row.get("plugin") or ""),
    ]
    chain = table(
        ("Time", "Receipt", "Tool", "Resolution", "Reason", "SQL"),
        [
            (
                when(item.get("at")),
                link(f"/admin/audit/{item['id']}", item["id"]),
                item.get("tool"),
                item.get("resolution"),
                item.get("refusal_reason") or "",
                str(item.get("sql") or "")[:300],
            )
            for item in data["chain"]
        ],
    )
    return page(
        ctx,
        f"Audit row {row.get('id')}",
        table(("", ""), rows),
        h2("Question"),
        tag("div", pre(row.get("question") or ""), class_="untrusted"),
        h2("SQL"),
        pre(row.get("sql") or ""),
        h2("Arguments"),
        tag("div", pre(row.get("arguments") or {}), class_="untrusted"),
        h2("Request chain"),
        chain,
        link("/admin/audit", "Back to the audit"),
    )


def clients(ctx):
    sessions = ctx.call("connected-clients")
    return page(
        ctx,
        "Connected clients",
        table(
            ("Person", "Client", "Connected", "Expires"),
            [
                (
                    s.get("name") or s.get("principal_id"),
                    s.get("client_name") or s.get("client_id"),
                    when(s.get("started_at")),
                    when(s.get("expires_at")),
                )
                for s in sessions
            ],
            empty="No connected clients.",
        ),
        hint(
            "People revoke their own clients at /connected-clients; disabling a person revokes all."
        ),
    )


def settings(ctx):
    values = ctx.call("settings")

    def editor(key, spec):
        if isinstance(spec["default"], bool):
            control = select("value", [("true", "on"), ("false", "off")], flag(values[key]))
        elif "choices" in spec:
            control = select("value", [(c, c) for c in spec["choices"]], values[key])
        else:
            control = field(
                "Value",
                "value",
                value=values[key],
                type_="number",
                min=str(spec["minimum"]),
                max=str(spec["maximum"]),
            )
        return form("/admin/settings", ctx.csrf, hidden("key", key), control, class_="inline")

    rows = [
        (
            key,
            "on" if values[key] is True else "off" if values[key] is False else values[key],
            "on"
            if spec["default"] is True
            else "off"
            if spec["default"] is False
            else spec["default"],
            "" if spec["minimum"] is None else f"{spec['minimum']}–{spec['maximum']}",
            spec["description"],
            editor(key, spec),
        )
        for key, spec in SETTINGS.items()
    ]
    return page(
        ctx,
        "Settings",
        table(("Setting", "Current", "Default", "Range", "Meaning", ""), rows),
    )


def settings_set(ctx):
    key, raw = ctx.form.get("key", ""), ctx.form.get("value", "")
    spec = SETTINGS.get(key)
    if spec is None:
        raise ValueError("unknown setting")
    if isinstance(spec["default"], bool):
        if raw not in {"true", "false"}:
            raise ValueError("choose on or off")
        value = raw == "true"
    elif "choices" in spec:
        if raw not in spec["choices"]:
            raise ValueError("choose " + " or ".join(spec["choices"]))
        value = raw
    else:
        if not raw.isdecimal():
            raise ValueError("enter a whole number")
        value = int(raw)
    ctx.call("settings/set", {"key": key, "value": value}, write=True)
    return done("/admin/settings", "settings/set")


SCREENS = [
    ("/admin", home, ["GET"]),
    ("/admin/users", users, ["GET"]),
    ("/admin/users/create", users_create, ["POST"]),
    ("/admin/users/{principal_id}/admin", principal_action("users/set-admin"), ["POST"]),
    ("/admin/users/{principal_id}/disable", principal_action("users/disable"), ["POST"]),
    ("/admin/users/{principal_id}/delete", principal_action("users/delete"), ["POST"]),
    ("/admin/services/create", services_create, ["POST"]),
    ("/admin/groups/create", groups_create, ["POST"]),
    ("/admin/groups/member", groups_member, ["POST"]),
    ("/admin/groups/bind", groups_bind, ["POST"]),
    ("/admin/groups/{group}/confirm", groups_confirm, ["POST"]),
    ("/admin/groups/{group}/unbind", groups_unbind, ["POST"]),
    ("/admin/groups/{group}/delete", groups_delete, ["POST"]),
    ("/admin/access", access, ["GET"]),
    ("/admin/access/grant", access_grant, ["POST"]),
    ("/admin/access/denied", access_denied, ["POST"]),
    ("/admin/access/reconcile", access_reconcile, ["POST"]),
    ("/admin/model", model, ["GET"]),
    ("/admin/model/field", model_field, ["GET"]),
    ("/admin/analytics", analytics, ["GET"]),
    ("/admin/gaps", gaps, ["GET"]),
    ("/admin/gaps/{gap_id}", gap, ["GET"]),
    ("/admin/gaps/{gap_id}", gap_status, ["POST"]),
    ("/admin/gaps/{gap_id}/confirm", gap_confirm, ["POST"]),
    ("/admin/audit", audit, ["GET"]),
    ("/admin/audit/{row_id}", audit_row, ["GET"]),
    ("/admin/clients", clients, ["GET"]),
    ("/admin/settings", settings, ["GET"]),
    ("/admin/settings", settings_set, ["POST"]),
]
