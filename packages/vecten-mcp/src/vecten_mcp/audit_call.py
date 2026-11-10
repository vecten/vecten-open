"""Request-local audit evidence shared by transport and warehouse execution."""

import copy
import time
from contextvars import ContextVar

import sqlglot
from sqlglot import exp

from vecten_mcp.audit import audit_statements, bound_sql, bound_sql_hash, receipt
from vecten_shared.osi.expressions import selected_expression

current_audit = ContextVar("vecten_audit_call", default=None)


class AuditCall:
    def __init__(self, runtime, caller, name, arguments):
        self.audit, self.caller = runtime.audit, caller
        self.started = time.monotonic()
        self.model = runtime.model.current if runtime.model else None
        model = self.model
        self.receipt = receipt(model.commit if model else None)
        self.kind = runtime.queries.kind if runtime.queries else "postgres"
        self.denied, self.redact = set(), True
        chain = self.audit.chain(caller, name, self.receipt["id"], now=self.receipt["issued_at"])
        self.row = {
            "id": self.receipt["id"],
            "at": self.receipt["issued_at"],
            "principal_id": caller.principal["id"],
            "principal_kind": caller.principal["kind"],
            "client_id": caller.client_id,
            "session_id": caller.session_id,
            "request_chain_id": chain,
            "tool": name,
            "arguments": copy.deepcopy(arguments),
            "question": arguments.get("question"),
            "sql": None,
            "sql_sha256": None,
            "model_commit": self.receipt["model"],
            "tables": [],
            "columns": [],
            "row_count": None,
            "duration_ms": 0,
            "error_class": None,
            "resolution": "agent_sql" if name == "sql-exec" else "compiled",
            "plugin": runtime.extensions.plugin_for(name),
        }

    def policy(self, runtime):
        denied = runtime.access.denied(self.caller.principal["id"]) if runtime.access else set()
        if self.model:
            denied_names = {name.casefold() for name in denied}
            for dataset, definition in getattr(self.model, "datasets", {}).items():
                for field in definition.get("fields", []):
                    if (dataset + "." + field["name"]).casefold() in denied_names:
                        tree = selected_expression(field, self.kind)
                        denied.update(column.name for column in tree.find_all(exp.Column))
        setting = runtime.state.get("settings", "redact")
        return denied, setting["value"] if setting else False

    def prepare(self, policy):
        self.denied, self.redact = policy
        arguments = self.row["arguments"]
        if self.row["tool"] == "sql-exec" and isinstance(arguments.get("sql"), str):
            self.record_query(arguments["sql"], arguments.get("parameters") or (), kind=self.kind)

    def record_query(self, sql, parameters=(), *, kind, executed=False, tables=None, columns=None):
        self.kind = kind
        self.row["sql"] = sql
        try:
            _, trees = audit_statements(sql, kind=kind)
            references = set()
            for tree in trees:
                for table in tree.find_all(exp.Table):
                    table = table.copy()
                    table.set("alias", None)
                    references.add(table.sql(dialect=kind))
            self.row["tables"] = sorted(references)
            self.row["columns"] = sorted(
                {column.sql(dialect=kind) for tree in trees for column in tree.find_all(exp.Column)}
            )
            if not isinstance(parameters, (list, tuple)):
                raise ValueError("SQL parameters must be a sequence of values")
            self.row["sql"] = bound_sql(sql, parameters, kind=kind)
        except ValueError, sqlglot.errors.SqlglotError:
            # Unparseable or unbound SQL cannot safely retain client-supplied values.
            self.redact = True
            self.row["sql"] = "[redacted: SQL could not be parsed or bound]"
        if tables is not None:
            self.row["tables"] = tables
        if columns is not None:
            self.row["columns"] = columns
        if executed:
            self.receipt["sql_sha256"] = bound_sql_hash(sql)
            self.row["sql_sha256"] = self.receipt["sql_sha256"]

    def refuse(self, reason):
        self.row.update(resolution="refused", refusal_reason=reason)

    def finish(self, result=None, error=None):
        if error is not None:
            cause = error.__cause__ or error
            timeout = (
                isinstance(cause, TimeoutError)
                or getattr(cause, "sqlstate", None) == "57014"
                or getattr(cause, "errno", None) == 604
            )
            self.row.update(
                resolution="error", error_class="timeout" if timeout else type(cause).__name__
            )
        elif (
            result is not None
            and getattr(result, "is_error", False)
            and self.row["resolution"] != "refused"
        ):
            self.row.update(resolution="error", error_class="ToolError")
        self.row["duration_ms"] = (time.monotonic() - self.started) * 1000
        if self.row["tool"] == "query_metrics" and self.row["resolution"] == "refused":
            self.audit.remember_refusal(self.caller, self.row["request_chain_id"], now=time.time())
        self.audit.enqueue(self.row, denied=self.denied, kind=self.kind, redact=self.redact)
        if result is not None:
            result.meta = {
                **(result.meta or {}),
                "vecten/receipt": dict(self.receipt),
                "vecten/request_chain_id": self.row["request_chain_id"],
            }
