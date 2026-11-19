"""One bounded execution path for compiled metrics and caller-authored read statements."""

import datetime
import decimal

from psycopg import sql as pg_sql

from vecten_mcp.audit import bound_sql, bound_sql_hash, receipt_time
from vecten_mcp.audit_call import current_audit
from vecten_mcp.failures import AppReadsNothing
from vecten_mcp.warehouse.base import check_account
from vecten_shared.osi.compiler import pyformat_statement
from vecten_shared.osi.sql_policy import read_statement


def json_value(value):
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def query_setting(state, name, default, maximum):
    saved = state.get("settings", name)
    value = saved["value"] if saved else default
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid query setting")
    return value


# The roles the session user holds through membership that none of the given roles grants.
STRAY_ROLES = (
    "SELECT r.rolname FROM pg_catalog.pg_roles r WHERE r.rolname <> current_user "
    "AND pg_catalog.pg_has_role(current_user, r.oid, 'USAGE') AND NOT EXISTS ("
    "SELECT FROM pg_catalog.unnest(%s::text[]) AS a(name) "
    "WHERE pg_catalog.pg_has_role(a.name, r.oid, 'USAGE'))"
)


class QueryService:
    def __init__(self, state, pools, *, kind):
        self.state, self.pools, self.kind = state, pools, kind
        # (principal id, groups) -> the groups an app principal queries with, or None for
        # any other principal (AppScopes.query_groups).
        self.narrow = None

    def setting(self, name, default, maximum):
        return query_setting(self.state, name, default, maximum)

    def execute(self, caller, text, parameters, receipt):
        call = current_audit.get()
        if call:
            receipt = call.receipt
            call.record_query(text, parameters, kind=self.kind)
        cap = self.setting("row_cap", 1000, 10000)
        timeout = self.setting("statement_timeout_seconds", 30, 3600)
        statement = read_statement(text, kind=self.kind, row_cap=cap)
        identifier = receipt["id"]
        receipt_time(identifier)
        resolved_sql = bound_sql(statement.sql, parameters, kind=self.kind)
        receipt = {**receipt, "sql_sha256": bound_sql_hash(resolved_sql)}
        if call:
            from vecten_mcp.feedback_signals import table_datasets

            call.record_query(
                resolved_sql,
                kind=self.kind,
                executed=True,
                tables=statement.tables,
                columns=statement.columns,
            )
            if not call.row.get("datasets"):
                call.row["datasets"] = table_datasets(statement.tables, call.model)
        query = f"/* vecten {identifier} */\n" + statement.sql
        narrowed = self.narrow(caller.principal["id"], caller.groups) if self.narrow else None
        if narrowed is not None and not narrowed:
            raise AppReadsNothing("this app admits no group, so its principal reads nothing")
        groups = caller.groups if narrowed is None else narrowed
        roles = {self.state.group(name)["warehouse_role"] for name in groups}
        check_account(caller.principal["warehouse_name"])
        if call:
            call.row["warehouse_attempted"] = True
        with self.pools.acquire(
            caller.principal, caller.session_id, caller.expires_at, roles
        ) as conn:
            if self.kind == "postgres":
                with conn.transaction():
                    conn.execute("SET TRANSACTION READ ONLY")
                    conn.execute(
                        pg_sql.SQL("SET LOCAL ROLE {}").format(
                            pg_sql.Identifier(caller.principal["warehouse_name"])
                        )
                    )
                    if narrowed is not None:
                        # The session user inherits every role granted to it, so an app
                        # principal holding one beyond its admitted groups' reads nothing.
                        stray = conn.execute(STRAY_ROLES, (sorted(roles),)).fetchall()
                        if stray:
                            raise AppReadsNothing(
                                "the app's principal holds roles beyond its admitted groups: "
                                + ", ".join(sorted(row[0] for row in stray))
                            )
                    conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)", (str(timeout * 1000),)
                    )
                    # psycopg reads every % as placeholder syntax once values are bound.
                    sent = pyformat_statement(query) if parameters else query
                    cursor = conn.execute(sent, parameters or None)
                    columns = [column[0] for column in cursor.description]
                    rows = cursor.fetchmany(cap)
            else:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"ALTER SESSION SET QUERY_TAG = 'vecten {identifier}'")
                    cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout}")
                    cursor.execute(query, parameters or None)
                    columns = [column[0] for column in cursor.description]
                    rows = cursor.fetchmany(cap)
                finally:
                    cursor.close()
        if call:
            call.row["row_count"] = len(rows)
        return {
            "rows": json_value(rows),
            "columns": columns,
            "sql": resolved_sql,
            "sql_template": statement.sql,
            "receipt": receipt,
            "tables": statement.tables,
            "referenced_columns": statement.columns,
        }
