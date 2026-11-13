"""The one administrative service behind the admin API, the admin tools and the panel."""

from sqlalchemy import select

from vecten_mcp.analytics import Analytics
from vecten_mcp.audit import masked
from vecten_mcp.auth.service import is_admin
from vecten_mcp.state import memberships

READS = frozenset(
    {
        "operations",
        "users",
        "services",
        "groups",
        "memberships",
        "grants",
        "denied",
        "connected-clients",
        "settings",
        "gaps",
        "gaps/read",
        "model",
        "model-health",
        "access-health",
        "usage",
        "analytics",
        "audit",
        "audit/read",
        "audit-status",
        "apps",
    }
)
WRITES = frozenset(
    {
        "gaps/confirm",
        "users/create",
        "users/disable",
        "users/delete",
        "users/set-admin",
        "users/password-link",
        "services/create",
        "services/disable",
        "services/delete",
        "groups/create",
        "groups/delete",
        "groups/member",
        "grant",
        "revoke",
        "denied/set",
        "settings/set",
        "reconcile",
        "gaps/status",
        "apps/connect",
    }
)
# Admin sessions read warehouse data and gap evidence that can carry instructions, so the
# agent surface gets reads and the one gap-status change, never identity or access changes.
TOOL_OPERATIONS = frozenset(
    {
        "gaps",
        "gaps/read",
        "gaps/status",
        "users",
        "groups",
        "usage",
        "model-health",
        "connected-clients",
        "analytics",
        "audit",
        "audit/read",
        "grants",
        "denied",
        "settings",
        "apps",
    }
)
SETTINGS = {
    "audit_retention_days": {
        "default": 90,
        "minimum": 1,
        "maximum": 3650,
        "description": "Days an audit row and its access events are kept.",
    },
    "redact": {
        "default": False,
        "minimum": None,
        "maximum": None,
        "description": "Hide questions, arguments and identifiers in stored and read audit.",
    },
    "session_idle_seconds": {
        "default": 900,
        "minimum": 30,
        "maximum": 3600,
        "description": "Seconds an idle per-person warehouse connection stays open.",
    },
    "row_cap": {
        "default": 1000,
        "minimum": 1,
        "maximum": 10000,
        "description": "Rows a query tool returns at most.",
    },
    "statement_timeout_seconds": {
        "default": 30,
        "minimum": 1,
        "maximum": 3600,
        "description": "Seconds a warehouse statement may run.",
    },
}
DBT_MARTS = "dbt/models/marts/"


class AdminRefused(PermissionError):
    pass


class OperationRefused(ValueError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def subject_of(data):
    return str(data.get("id", data.get("target", data.get("name", ""))))[:200]


def number(value, name):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number of seconds since the epoch") from exc


def integer(value, name):
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    if value is not None and type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


class AdminService:
    def __init__(self, runtime):
        from vecten_mcp.tool_service import ToolService

        self.runtime, self.state = runtime, runtime.state
        self.tools = getattr(runtime, "tool_service", None) or ToolService(runtime)

    def authorize(self, caller):
        if not is_admin(caller) or not any(
            p["admin"] and p["enabled"] for p in self.state.principals()
        ):
            raise AdminRefused("human admin required; configure the first admin and sign in")

    def check(self, operation, *, surface, write):
        if surface == "mcp" and operation not in TOOL_OPERATIONS:
            raise OperationRefused("operation is not available to agents", 404)
        if not write and operation not in READS:
            raise OperationRefused("mutation requires POST", 405)
        if write and operation not in WRITES:
            raise OperationRefused("unknown mutation", 404)

    def event(self, principal_id, operation, subject, outcome, *, surface):
        name = operation if surface == "api" else f"{surface}/{operation}"
        self.runtime.oauth.event(principal_id, name, subject, outcome)

    def call(self, caller, operation, data, *, surface, write):
        subject, outcome = subject_of(data), "denied"
        try:
            self.authorize(caller)
            outcome = "error"
            self.check(operation, surface=surface, write=write)
            result = self.dispatch(caller, operation, data)
            outcome = "allowed"
            return result
        finally:
            self.event(caller.principal["id"], operation, subject, outcome, surface=surface)

    def dispatch(self, caller, operation, data):
        runtime, state = self.runtime, self.state
        admin, access, auth = runtime.administration, runtime.access, runtime.oauth
        if operation in {
            "services",
            "groups",
            "connected-clients",
            "model-health",
            "gaps",
            "gaps/read",
            "usage",
        }:
            return self.tools.admin_read(caller, operation, data)
        if operation == "users":
            people = self.tools.admin_read(caller, "users", data)
            if data.get("kind", "human") == "all":
                return people
            return [p for p in people if p["kind"] == "human"]
        if operation == "gaps/status":
            return self.tools.set_gap_status(caller, data["id"], data["status"])
        if operation == "gaps/confirm":
            return self.tools.confirm_gap_fix(caller, data["id"])
        if operation == "operations":
            return {"read": sorted(READS), "write": sorted(WRITES)}
        if operation == "analytics":
            return Analytics(runtime).view(data.get("view", ""), integer(data.get("days"), "days"))
        if operation == "audit":
            return self.audit(data)
        if operation == "audit/read":
            return self.audit_row(data["id"])
        if operation == "model":
            return self.model(integer(data.get("days"), "days"))
        if operation == "users/create":
            return admin.create_person(
                data["email"],
                data["name"],
                warehouse_override=data.get("warehouse_name"),
                adopt=data.get("adopt") is True,
            )
        if operation == "services/create":
            return admin.create_service(data["name"], adopt=data.get("adopt") is True)
        if operation in {"users/disable", "services/disable"}:
            return admin.disable(data["id"])
        if operation in {"users/delete", "services/delete"}:
            return admin.delete_principal(data["id"])
        if operation == "users/set-admin":
            if not isinstance(data.get("admin"), bool):
                raise ValueError("admin must be a boolean")
            return admin.set_admin(data["id"], data["admin"])
        if operation == "users/password-link":
            principal = admin._require(data["id"])
            if not hasattr(auth.backend, "setup_link"):
                raise ValueError("password backend is not selected")
            return {
                "url": auth.issuer + "/set-password",
                "setup_token": auth.backend.setup_link(principal["email"]),
            }
        if operation == "memberships":
            with state.transaction() as conn:
                return [dict(row) for row in conn.execute(select(memberships)).mappings()]
        if operation == "groups/create":
            return access.create_group(data["name"])
        if operation == "groups/delete":
            return access.delete_group(data["name"])
        if operation == "groups/member":
            if not isinstance(data.get("member"), bool):
                raise ValueError("member must be a boolean")
            return access.membership(data["group"], data["id"], data["member"])
        if operation == "grants":
            return access.list_grants()
        if operation in {"grant", "revoke"}:
            method = access.grant if operation == "grant" else access.revoke
            return method(data["group"], data["kind"], data["target"])
        if operation == "denied":
            return [{"group": group, **row} for group, row in state.scan("denied")]
        if operation == "denied/set":
            return access.set_denied(data["group"], data["columns"])
        if operation == "reconcile":
            return access.reconcile()
        if operation == "access-health":
            return access.health() if access else {"checked_at": None, "failures": []}
        if operation == "settings":
            return self.settings()
        if operation == "settings/set":
            return self.set_setting(data["key"], data["value"])
        if operation == "apps":
            apps = getattr(runtime, "apps", None)
            return apps.listing(auth.issuer) if apps else []
        if operation == "apps/connect":
            return admin.connect_app(
                getattr(runtime, "apps", None), data["name"], adopt=data.get("adopt") is True
            )
        if operation == "audit-status":
            if not runtime.audit:
                raise RuntimeError("audit service unavailable")
            return runtime.audit.stats()
        raise ValueError("unknown operation")

    def settings(self):
        return {
            key: (self.state.get("settings", key) or {"value": spec["default"]})["value"]
            for key, spec in SETTINGS.items()
        }

    def set_setting(self, key, value):
        spec = SETTINGS.get(key)
        if spec is None:
            raise ValueError("unknown setting or invalid value")
        if type(value) is not type(spec["default"]) or (
            spec["minimum"] is not None and not spec["minimum"] <= value <= spec["maximum"]
        ):
            raise ValueError("setting is outside the permitted range")
        pools = getattr(self.runtime, "pools", None)
        if key == "session_idle_seconds" and pools:
            pools.idle_timeout = value
        self.state.put("settings", key, {"value": value})
        return {"key": key, "value": value}

    def audit(self, data):
        rows = self.runtime.audit.browse(
            principal_id=data.get("principal_id") or None,
            tool=data.get("tool") or None,
            resolution=data.get("resolution") or None,
            since=number(data.get("since"), "since"),
            until=number(data.get("until"), "until"),
            before=number(data.get("before"), "before"),
            limit=integer(data.get("limit"), "limit") or 50,
        )
        redact = self.tools.redacted()
        return [masked(row, redact) for row in rows]

    def audit_row(self, identifier):
        row = self.runtime.audit.find(identifier)
        if row is None:
            raise ValueError("audit row not found")
        redact = self.tools.redacted()
        chain = self.runtime.audit.chain_rows(row.get("request_chain_id") or identifier)
        return {"row": masked(row, redact), "chain": [masked(item, redact) for item in chain]}

    def model(self, days=None):
        service = getattr(self.runtime, "model", None)
        current = service.current if service else None
        health = (
            service.health() if service else {"status": "not loaded", "errors": [], "warnings": []}
        )
        usage = Analytics(self.runtime).view("field_usage", days)
        fields = {(f["dataset"], f["field"]): f["count"] for f in usage["fields"]}
        metrics = {m["name"]: m["count"] for m in usage["metrics"]}
        platform = bool(getattr(service, "platform", False))

        def described_in(dataset, field):
            if platform:
                return f"schema.yml under {DBT_MARTS}: model {dataset}, column {field}"
            return f"osi/datasets.json: dataset {dataset}, field {field}"

        return {
            "commit": current.commit if current else None,
            "health": health,
            "semantic_skill": "semantic",
            "usage_days": usage["days"],
            "datasets": [
                {
                    "name": name,
                    "description": definition.get("description", ""),
                    "source": definition.get("source"),
                    "fields": [
                        {
                            "name": field["name"],
                            "description": field.get("description", ""),
                            "classification": field.get("classification"),
                            "count": fields.get((name, field["name"]), 0),
                            "described_in": described_in(name, field["name"]),
                        }
                        for field in definition.get("fields", [])
                    ],
                }
                for name, definition in sorted((current.datasets if current else {}).items())
            ],
            "metrics": [
                {
                    "name": name,
                    "dataset": current.homes.get(name),
                    "description": metric.get("description", ""),
                    "count": metrics.get(name, 0),
                    "described_in": f"osi/metrics.json: metric {name}",
                }
                for name, metric in sorted((current.metrics if current else {}).items())
            ],
        }
