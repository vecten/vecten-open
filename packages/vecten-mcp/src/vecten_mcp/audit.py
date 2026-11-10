"""Bounded asynchronous audit delivery and privacy-preserving receipt records."""

import copy
import hashlib
import logging
import queue
import re
import secrets
import threading
import time
from collections import Counter, OrderedDict, defaultdict, deque

import sqlglot
from sqlalchemy import func, insert, select
from sqlglot import exp

from vecten_mcp.failures import describe_error
from vecten_mcp.state import audit_log
from vecten_shared.osi.sql_policy import read_statement

logger = logging.getLogger(__name__)
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SCALARS = ("id", "tool", "principal_id", "resolution", "refusal_reason", "question")
LISTS = ("metrics", "datasets", "columns")
PROJECTION = (*SCALARS, "at", *LISTS, "request_chain_id")
PAGE_CAP = 200
CHAIN_CAP = 100


def masked(row, redact):
    """Apply the read-time redaction toggle to a stored row that predates it."""
    if not redact:
        return row
    return {**row, "question": "[redacted]", "arguments": "[redacted]", "refusal_context": {}}


def receipt(model, *, now=None):
    now = time.time() if now is None else now
    number = (int(now * 1000) << 80) | int.from_bytes(secrets.token_bytes(10), "big")
    identifier = "".join(
        ALPHABET[(number >> (5 * position)) & 31] for position in reversed(range(26))
    )
    return {"id": identifier, "model": model, "issued_at": now, "sql_sha256": None}


def receipt_time(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[0-7][0-9A-HJKMNP-TV-Z]{25}", identifier
    ):
        raise ValueError("receipt_id must be a ULID")
    number = 0
    for char in identifier[:10]:
        number = number * 32 + ALPHABET.index(char)
    return number / 1000


def audit_statements(sql, *, kind):
    if not isinstance(sql, str):
        raise ValueError("SQL must be text")
    tokens = sqlglot.tokenize(sql, read=kind)
    if tokens and tokens[0].text.upper() == "EXPLAIN":
        statement = read_statement(sql, kind=kind, row_cap=None)
        inner = statement.tree.sql(dialect=kind)
        return statement.sql[: -len(inner)], [statement.tree]
    if kind == "postgres" and tokens and tokens[0].text.upper() == "SHOW":
        return read_statement(sql, kind=kind, row_cap=None).sql, []
    # Unsupported syntax may be logged by SQLGlot; never let that diagnostic include input.
    trees = [
        tree for tree in sqlglot.parse(sql, read=kind, error_message_context=0) if tree is not None
    ]
    if any(tree.find(exp.Command) for tree in trees):
        raise ValueError("opaque SQL cannot be audited safely")
    return "", trees


def bound_sql(sql, parameters=(), *, kind="postgres"):
    if not parameters:
        return sql
    prefix, trees = audit_statements(sql, kind=kind)
    if len(trees) != 1:
        raise ValueError("SQL must contain one statement")
    tree = trees[0]
    placeholders = [node for node in tree.walk(bfs=False) if isinstance(node, exp.Placeholder)]
    if len(placeholders) != len(parameters):
        raise ValueError("SQL parameter count mismatch")
    for node, value in zip(placeholders, parameters, strict=True):
        node.replace(exp.convert(value))
    return prefix + tree.sql(dialect=kind)


def bound_sql_hash(sql, parameters=(), *, kind="postgres"):
    return hashlib.sha256(bound_sql(sql, parameters, kind=kind).encode()).hexdigest()


def denied_expressions(tree, names):
    """Follow expression and alias dependencies once, including cyclic alias names."""
    dependents = defaultdict(set)
    nodes = list(tree.walk())
    stars = set()
    seeds = set(names)
    for node in reversed(nodes):
        children = list(node.iter_expressions())
        for child in children:
            dependents[id(child)].add(id(node))
        if isinstance(node, exp.Star) or any(id(child) in stars for child in children):
            stars.add(id(node))
        if isinstance(node, exp.Column):
            dependents[node.name.casefold()].add(id(node))
        if isinstance(node, exp.Alias):
            dependents[id(node.this)].add(node.alias.casefold())
        if isinstance(node, (exp.CTE, exp.Subquery)):
            for alias, projection in zip(
                node.alias_column_names, getattr(node.this, "selects", []), strict=False
            ):
                dependents[id(projection)].add(alias.casefold())
                if id(projection) in stars:
                    seeds.add(alias.casefold())
    affected = set(seeds)
    pending = deque(seeds)
    while pending:
        for dependent in dependents[pending.popleft()]:
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    return affected


def redact_row(row, *, denied, kind, redact=False):
    row = copy.deepcopy(row)
    names = {name.casefold().rsplit(".", 1)[-1] for name in denied}
    values = set()
    if names and row.get("sql"):
        try:
            prefix, trees = audit_statements(row["sql"], kind=kind)
            for tree in trees:
                affected = denied_expressions(tree, names)
                names.update(name for name in affected if isinstance(name, str))
                scopes = (exp.Predicate, exp.Func, exp.Case, exp.If)
                literals = (
                    exp.Literal,
                    exp.ByteString,
                    exp.RawString,
                    exp.National,
                    exp.HexString,
                    exp.BitString,
                    exp.UnicodeString,
                    exp.Boolean,
                )
                pending = [(tree, False)]
                while pending:
                    node, hide = pending.pop()
                    node.comments = None
                    hide = hide or (
                        id(node) in affected
                        and isinstance(node, scopes)
                        and not isinstance(node, exp.Connector)
                    )
                    if hide and isinstance(node, literals):
                        values.add(str(node.this))
                        node.replace(exp.Literal.string("[redacted]"))
                    else:
                        pending.extend((child, hide) for child in node.iter_expressions())
            row["sql"] = prefix + "; ".join(tree.sql(dialect=kind) for tree in trees)
        except ValueError, sqlglot.errors.SqlglotError:
            row["sql"] = "[redacted: SQL could not be parsed]"
            redact = True

    def find_filters(value):
        if isinstance(value, dict):
            field = value.get("field", value.get("column", ""))
            if isinstance(field, str) and field.casefold().rsplit(".", 1)[-1] in names:
                raw = value.get("value", value.get("values"))
                for item in raw if isinstance(raw, list) else [raw]:
                    if item is not None:
                        values.add(str(item))
            for item in value.values():
                find_filters(item)
        elif isinstance(value, list):
            for item in value:
                find_filters(item)

    find_filters(row.get("arguments"))

    def scrub(value):
        if isinstance(value, dict):
            return {
                key: row.get("sql") if key == "sql" and row.get("sql") else scrub(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(values, key=len, reverse=True):
                if secret:
                    value = value.replace(secret.replace("'", "''"), "[redacted]")
                    value = value.replace(secret, "[redacted]")
            return value
        return "[redacted]" if value is not None and str(value) in values else value

    for key in ("arguments", "question"):
        row[key] = "[redacted]" if redact else scrub(row.get(key))
    row["refusal_context"] = {} if redact else scrub(row.get("refusal_context", {}))
    if redact:
        for key in ("datasets", "columns", "names"):
            row[key] = []
    row["redacted"] = bool(redact or values)
    return row


class AuditService:
    def __init__(self, state, *, capacity=1024, start=True, sink=None):
        self.state, self.sink = state, sink
        self.queue = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.written = self.dropped = 0
        self._chains = OrderedDict()
        if start:
            self.start()

    def chain(self, caller, tool, identifier, *, now):
        key = (caller.principal["id"], caller.client_id, caller.session_id)
        with self._lock:
            for expired in [key for key, (_, until) in self._chains.items() if until <= now]:
                del self._chains[expired]
            if tool in {"query_metrics", "sql-exec"}:
                previous = self._chains.pop(key, None)
                if tool == "sql-exec" and previous:
                    return previous[0]
        return identifier

    def remember_refusal(self, caller, chain, *, now):
        key = (caller.principal["id"], caller.client_id, caller.session_id)
        with self._lock:
            self._chains.pop(key, None)
            self._chains[key] = (chain, min(now + 300, caller.expires_at))
            while len(self._chains) > 1024:
                self._chains.popitem(last=False)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="vecten-audit", daemon=True)
            self._thread.start()

    def _drop(self, reason):
        with self._lock:
            self.dropped += 1
        logger.error("MCP audit row dropped (%s)", reason)

    def enqueue(self, row, *, denied=None, kind="postgres", redact=False):
        try:
            if self._stop.is_set():
                raise queue.Full
            policy = (
                None if denied is None else {"denied": set(denied), "kind": kind, "redact": redact}
            )
            self.queue.put_nowait((copy.deepcopy(row), policy))
            return True
        except queue.Full:
            self._drop("queue unavailable")
            return False

    def write(self, row):
        with self.state.transaction() as conn:
            conn.execute(insert(audit_log).values(id=row["id"], at=row["at"], body=row))

    def _deliver(self, item):
        row, policy = item
        try:
            if policy is not None:
                row = redact_row(row, **policy)
            self.write(row)
        except Exception as exc:
            self._drop(type(exc).__name__)
        else:
            with self._lock:
                self.written += 1
            if self.sink:
                try:
                    self.sink(copy.deepcopy(row))
                except Exception as exc:
                    logger.error("MCP audit sink failed: %s", describe_error(exc))
        finally:
            self.queue.task_done()

    def _run(self):
        while not self._stop.is_set() or not self.queue.empty():
            try:
                row = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._deliver(row)

    def flush(self):
        if self._thread:
            self.queue.join()
        else:
            while True:
                try:
                    row = self.queue.get_nowait()
                except queue.Empty:
                    break
                self._deliver(row)

    def find(self, identifier):
        with self.state.transaction() as conn:
            return conn.execute(
                select(audit_log.c.body).where(audit_log.c.id == identifier)
            ).scalar_one_or_none()

    def rows(self, *, since=0):
        with self.state.transaction() as conn:
            return list(
                conn.execute(
                    select(audit_log.c.body).where(audit_log.c.at >= since).order_by(audit_log.c.at)
                ).scalars()
            )

    def projection(self, *, since, until):
        """Analytics columns only; statement text and arguments stay in the database."""
        body = audit_log.c.body
        columns = [audit_log.c.at.label("at")]
        columns += [body[key].as_string().label(key) for key in (*SCALARS, "request_chain_id")]
        columns += [body[key].label(key) for key in LISTS]
        statement = (
            select(*columns)
            .where(audit_log.c.at >= since, audit_log.c.at <= until)
            .order_by(audit_log.c.at)
        )
        with self.state.transaction() as conn:
            rows = [dict(row) for row in conn.execute(statement).mappings()]
        for row in rows:
            for key in LISTS:
                if not isinstance(row[key], list):
                    row[key] = []
        return rows

    def browse(
        self,
        *,
        principal_id=None,
        tool=None,
        resolution=None,
        since=None,
        until=None,
        before=None,
        limit=50,
    ):
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        body = audit_log.c.body
        statement = select(body).order_by(audit_log.c.at.desc()).limit(min(limit, PAGE_CAP))
        for key, value in (
            ("principal_id", principal_id),
            ("tool", tool),
            ("resolution", resolution),
        ):
            if value is not None:
                statement = statement.where(body[key].as_string() == value)
        if since is not None:
            statement = statement.where(audit_log.c.at >= since)
        if until is not None:
            statement = statement.where(audit_log.c.at <= until)
        if before is not None:
            statement = statement.where(audit_log.c.at < before)
        with self.state.transaction() as conn:
            return list(conn.execute(statement).scalars())

    def chain_rows(self, chain_id):
        statement = (
            select(audit_log.c.body)
            .where(audit_log.c.body["request_chain_id"].as_string() == chain_id)
            .order_by(audit_log.c.at)
            .limit(CHAIN_CAP)
        )
        with self.state.transaction() as conn:
            return list(conn.execute(statement).scalars())

    def usage(self, *, since, until):
        tool, principal, resolution, reason = (
            audit_log.c.body[key].as_string()
            for key in ("tool", "principal_id", "resolution", "refusal_reason")
        )
        statement = (
            select(tool, principal, resolution, reason, func.count())
            .where(
                audit_log.c.at >= since,
                audit_log.c.at <= until,
                tool.in_(("query_metrics", "sql-exec")),
            )
            .group_by(tool, principal, resolution, reason)
        )
        by_tool, by_principal, refusals = Counter(), Counter(), Counter()
        with self.state.transaction() as conn:
            for tool_name, person, outcome, refusal, count in conn.execute(statement):
                by_tool[tool_name] += count
                by_principal[person] += count
                if outcome == "refused":
                    refusals[refusal or "unknown"] += count
        return {
            "questions": sum(by_tool.values()),
            "by_tool": dict(by_tool),
            "by_principal": dict(by_principal),
            "refusals": dict(refusals),
        }

    def stats(self):
        with self._lock:
            return {"queued": self.queue.qsize(), "written": self.written, "dropped": self.dropped}

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        else:
            self.flush()
