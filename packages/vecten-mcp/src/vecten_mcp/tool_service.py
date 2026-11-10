"""Shared feedback, receipt and administrative read operations over MCP state."""

import json
import time

from vecten_mcp.audit import bound_sql_hash, receipt, receipt_time
from vecten_mcp.auth.limits import RateLimiter
from vecten_mcp.auth.service import is_admin
from vecten_mcp.queries import query_setting
from vecten_shared.osi.sql_policy import read_statement

CATEGORIES = (
    "wrong value",
    "missing metric",
    "missing dataset",
    "wrong definition",
    "access denied",
)
RAISED_BY = ("user", "agent")
STATUSES = ("open", "triaged", "dismissed", "resolved")
TEXT_CAP = 4096
SQL_CAP = 16384
CALL_CAP = 20


def text(value, cap=TEXT_CAP):
    return value[:cap] if isinstance(value, str) else ""


def evidence(row, *, redact=False):
    source = row.get("evidence", row)
    if not isinstance(source, dict):
        source = {}
    result = {"untrusted": True}
    for name in ("question", "expected", "returned", "agent_sql"):
        result[name] = (
            "[redacted]"
            if redact
            else text(source.get(name), SQL_CAP if name == "agent_sql" else TEXT_CAP)
        )
    calls = source.get("tool_calls", [])
    result["tool_calls"] = []
    for item in calls[:CALL_CAP] if isinstance(calls, list) else []:
        # Stored typed evidence must not be JSON-encoded again on every read.
        value = (
            item.get("text")
            if isinstance(item, dict) and item.get("type") == "tool_call"
            else json.dumps(item, ensure_ascii=False)
        )
        result["tool_calls"].append(
            {
                "type": "tool_call",
                "text": "[redacted]" if redact else text(value),
            }
        )
    return result


def require_admin(caller):
    if not is_admin(caller):
        raise PermissionError("human admin required")


class ToolService:
    def __init__(self, runtime, *, clock=time.time):
        self.runtime, self.state, self.clock = runtime, runtime.state, clock
        self.feedback_limit = RateLimiter(limit=10, window=300, clock=clock)

    def redacted(self):
        return bool((self.state.get("settings", "redact") or {}).get("value", False))

    def report(self, caller, *, question, expected, returned, tool_calls, category, raised_by):
        if category not in CATEGORIES:
            raise ValueError("category must be one of: " + ", ".join(CATEGORIES))
        if raised_by not in RAISED_BY:
            raise ValueError("raised_by must be one of: " + ", ".join(RAISED_BY))
        if not all(isinstance(value, str) for value in (question, expected, returned)):
            raise ValueError("question, expected and returned must be text")
        if not isinstance(tool_calls, list) or not all(
            isinstance(item, dict) for item in tool_calls
        ):
            raise ValueError("tool_calls must be a list of objects")
        json.dumps(tool_calls, allow_nan=False)
        redact = self.redacted()
        body = evidence(
            {
                "question": question,
                "expected": expected,
                "returned": returned,
                "tool_calls": tool_calls,
            },
            redact=redact,
        )
        self.feedback_limit.hit(caller.principal["id"])
        from vecten_mcp.feedback import retention_seconds
        from vecten_mcp.feedback_signals import report_context

        model = self.runtime.model.current if self.runtime.model else None
        context = {} if redact else report_context(tool_calls, model)
        stamp = receipt(None, now=self.clock())
        self.state.put(
            "feedback",
            stamp["id"],
            {
                "id": stamp["id"],
                "at": stamp["issued_at"],
                "principal_id": caller.principal["id"],
                "category": category,
                "raised_by": raised_by,
                "evidence": body,
                **context,
            },
            expires_at=self.clock() + retention_seconds(self.state),
        )
        return {"id": stamp["id"], "status": "accepted"}

    def gap_view(self, identifier, row):
        redact = self.redacted()

        def names(key):
            if redact:
                return []
            values = row.get(key, [])
            return (
                [text(v, 256) for v in values[:100] if isinstance(v, str)]
                if isinstance(values, list)
                else []
            )

        return {
            "id": identifier,
            "category": text(row.get("category"), 100),
            "reason": text(row.get("reason"), 100),
            "datasets": names("datasets"),
            "columns": names("columns"),
            "occurrence_count": row.get("occurrence_count", 1),
            "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"),
            "status": row.get("status", "open"),
            "delivery_status": row.get("delivery_status"),
            "pull_request": row.get("pull_request"),
            "verification": row.get("verification"),
            "unreviewed_fix": row.get("unreviewed_fix"),
            "confirmation": row.get("confirmation"),
            "resolved_commit": row.get("resolved_commit"),
            "names": names("names"),
            "evidence": evidence(row, redact=redact),
        }

    def set_gap_status(self, caller, gap_id, status):
        require_admin(caller)
        if status not in STATUSES:
            raise ValueError("status must be one of: " + ", ".join(STATUSES))
        with self.state.transaction() as conn:
            row = self.state.get("gaps", gap_id, conn=conn, lock=True)
            if row is None:
                raise ValueError("gap not found")
            row["status"] = status
            self.state.put("gaps", gap_id, row, conn=conn)
        return self.gap_view(gap_id, row)

    def confirm_gap_fix(self, caller, gap_id):
        """Stand in for the review of the gap's unreviewed merged fix, for that merge only."""
        require_admin(caller)
        with self.state.transaction() as conn:
            row = self.state.get("gaps", gap_id, conn=conn, lock=True)
            if row is None:
                raise ValueError("gap not found")
            fix = row.get("unreviewed_fix")
            if not fix or row.get("status") == "resolved":
                raise ValueError("the gap has no unreviewed merged fix to confirm")
            row["confirmation"] = {
                "merge_commit": fix["merge_commit"],
                "principal_id": caller.principal["id"],
                "email": caller.principal["email"],
                "at": self.clock(),
            }
            self.state.put("gaps", gap_id, row, conn=conn)
        return self.gap_view(gap_id, row)

    def verify(self, caller, identifier):
        issued = receipt_time(identifier)
        row = self.runtime.audit.find(identifier)
        if row and row.get("principal_id") != caller.principal["id"] and not is_admin(caller):
            return {"status": "not_found"}
        days = (self.state.get("settings", "audit_retention_days") or {}).get("value", 90)
        if (row["at"] if row else issued) <= self.clock() - days * 86400:
            return {"status": "expired"}
        if row is None:
            return {"status": "not_found"}
        identical = None
        model = self.runtime.model.current if self.runtime.model else None
        if (
            row.get("tool") == "query_metrics"
            and row.get("resolution") == "compiled"
            and model
            and not row.get("redacted")
            and not self.redacted()
            and isinstance(row.get("arguments"), dict)
        ):
            denied = (
                self.runtime.access.denied(caller.principal["id"]) if self.runtime.access else set()
            )
            arguments = {
                key: value
                for key, value in row["arguments"].items()
                if key in {"metrics", "dimensions", "time", "filters", "order_by", "limit"}
                and value is not None
            }
            try:
                cap = query_setting(self.state, "row_cap", 1000, 10000)
                compiled = model.compile(arguments, denied=denied, row_cap=cap)
                identical = False
                if compiled["resolution"] == "compiled":
                    statement = read_statement(compiled["sql"], kind=model.kind, row_cap=cap)
                    identical = bound_sql_hash(
                        statement.sql, compiled["parameters"], kind=model.kind
                    ) == row.get("sql_sha256")
            except ValueError, KeyError, TypeError:
                identical = None
        return {
            "status": "found",
            "question": "[redacted]" if self.redacted() else row.get("question"),
            "tool": row.get("tool"),
            "sql": row.get("sql"),
            "resolution": row.get("resolution"),
            "model_commit": row.get("model_commit"),
            "person": row.get("principal_id"),
            "time": row["at"],
            "compiles_identically": identical,
        }

    def admin_read(self, caller, operation, data):
        require_admin(caller)
        if operation == "gaps":
            status = data.get("status", "open")
            if status not in {*STATUSES, "all"}:
                raise ValueError("invalid gap status")
            return [
                self.gap_view(key, row)
                for key, row in self.state.scan("gaps")
                if status == "all" or row.get("status", "open") == status
            ][:100]
        if operation == "gaps/read":
            row = self.state.get("gaps", data["id"])
            if row is None:
                raise ValueError("gap not found")
            return self.gap_view(data["id"], row)
        if operation in {"users", "services"}:
            keys = (
                "id",
                "name",
                "email",
                "kind",
                "admin",
                "enabled",
                "warehouse_name",
                "provisioned",
                "adopted",
            )
            return [
                {k: p[k] for k in keys}
                for p in self.state.principals()
                if operation == "users" or p["kind"] == "service"
            ]
        if operation == "groups":
            return self.runtime.access.list_groups()
        if operation == "connected-clients":
            names = {p["id"]: p["name"] for p in self.state.principals()}
            clients = dict(self.state.scan("clients"))
            return [
                {
                    **{
                        key: row.get(key)
                        for key in ("principal_id", "client_id", "started_at", "expires_at")
                    },
                    "name": names.get(row.get("principal_id")),
                    "client_name": (clients.get(row.get("client_id")) or {}).get("client_name"),
                }
                for _, row in self.state.scan("sessions")
                if row["expires_at"] > self.runtime.oauth.clock()
            ]
        if operation == "model-health":
            model = getattr(self.runtime, "model", None)
            return (
                model.health() if model else {"status": "not loaded", "errors": [], "warnings": []}
            )
        if operation == "usage":
            days = data.get("days", 7)
            if isinstance(days, str) and days.isdecimal():
                days = int(days)
            if type(days) is not int or not 1 <= days <= 90:
                raise ValueError("days must be an integer from 1 to 90")
            until = self.clock()
            since = until - days * 86400
            return {
                "since": since,
                "until": until,
                **self.runtime.audit.usage(since=since, until=until),
                "open_gaps": sum(
                    row.get("status", "open") in {"open", "triaged"}
                    for _, row in self.state.scan("gaps")
                ),
            }
        raise ValueError("unknown read operation")
