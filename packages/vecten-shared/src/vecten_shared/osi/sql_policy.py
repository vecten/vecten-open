"""Parse one read statement before it reaches a principal's warehouse connection."""

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

EXPLAIN_OPTIONS = frozenset(
    {
        "VERBOSE",
        "COSTS",
        "SETTINGS",
        "BUFFERS",
        "WAL",
        "TIMING",
        "SUMMARY",
        "FORMAT",
        "GENERIC_PLAN",
        "SERIALIZE",
        "TRUE",
        "FALSE",
        "ON",
        "OFF",
        "TEXT",
        "XML",
        "JSON",
        "YAML",
        "USING",
    }
)


@dataclass(frozen=True)
class ReadStatement:
    sql: str
    tree: object
    tables: list[str]
    columns: list[str]


def _references(tree, kind):
    tables = set()
    for scope in traverse_scope(tree) or []:
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                table = source.copy()
                table.set("alias", None)
                tables.add(table.sql(dialect=kind))
    columns = {column.sql(dialect=kind) for column in tree.find_all(exp.Column)}
    return sorted(tables), sorted(columns)


def read_statement(text, *, kind, row_cap=1000):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("SQL must be a nonempty string")
    if row_cap is not None and (type(row_cap) is not int or row_cap < 1):
        raise ValueError("row cap must be positive")
    try:
        tokens = sqlglot.tokenize(text, read=kind)
        if not tokens:
            raise ValueError("SQL must contain one read statement")
        text = text[tokens[0].start :]
        first = tokens[0].text.upper()
        if first not in {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}:
            raise ValueError("only SELECT, WITH, SHOW, DESCRIBE and EXPLAIN are allowed")
        if first == "EXPLAIN":
            tail = text[len("EXPLAIN") :].strip()
            end = re.search(r"\b(SELECT|WITH)\b", tail, re.IGNORECASE)
            if end is None:
                raise ValueError("EXPLAIN must describe a read query")
            options = tail[: end.start()]
            words = re.findall(r"[A-Za-z_]+", options.upper())
            if "ANALYZE" in words:
                raise ValueError("EXPLAIN ANALYZE is not allowed")
            if set(words) - EXPLAIN_OPTIONS or re.sub(r"[A-Za-z_\s(),]", "", options):
                raise ValueError("unsupported EXPLAIN options")
            inner = read_statement(tail[end.start() :], kind=kind, row_cap=row_cap)
            return ReadStatement(
                "EXPLAIN " + options + inner.sql, inner.tree, inner.tables, inner.columns
            )
        if first == "SHOW" and kind == "postgres":
            if not re.fullmatch(
                r"SHOW\s+(?:[A-Za-z_][A-Za-z0-9_.]*|TIME ZONE|TRANSACTION ISOLATION LEVEL)\s*;?",
                text,
                re.IGNORECASE,
            ):
                raise ValueError("SHOW must name one setting")
            return ReadStatement(text.rstrip(";"), None, [], [])
        statements = [
            tree
            for tree in sqlglot.parse(text, read=kind, error_message_context=0)
            if tree is not None
        ]
        if len(statements) != 1:
            raise ValueError("exactly one SQL statement is allowed")
        tree = statements[0]
        if not isinstance(
            tree, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Show, exp.Describe)
        ):
            raise ValueError("SQL is not a supported read statement")
        if any(tree.find_all(exp.DML, exp.DDL, exp.Into, exp.Command, exp.Lock)):
            raise ValueError("writes and locking are not allowed inside read statements")
        if any(
            isinstance(node, exp.Anonymous) and node.name.casefold() == "set_config"
            for node in tree.find_all(exp.Func)
        ):
            raise ValueError("session configuration functions are not allowed")
        if isinstance(tree, exp.Query) and row_cap is not None:
            limit = tree.args.get("limit")
            literal = limit.expression if limit else None
            count = (
                int(literal.this)
                if isinstance(literal, exp.Literal) and literal.is_int
                else row_cap
            )
            tree = tree.limit(min(max(count, 0), row_cap))
        tables, columns = _references(tree, kind)
        return ReadStatement(tree.sql(dialect=kind), tree, tables, columns)
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError("SQL could not be parsed as one read statement") from exc


def source_sql(text, kind):
    """Validate a source without capping the input rows of an aggregate."""
    try:
        parsed = sqlglot.parse(text, read=kind, error_message_context=0)
        if len(parsed) != 1 or parsed[0] is None:
            raise ValueError("dataset source must contain one relation")
        source = parsed[0]
        if isinstance(source, exp.Subquery):
            source = source.this
        if isinstance(source, exp.Query):
            statement = read_statement(source.sql(dialect=kind), kind=kind, row_cap=None)
            return "(" + statement.sql + ")"
        parsed = sqlglot.parse("SELECT * FROM " + text, read=kind)
        if len(parsed) != 1:
            raise ValueError("dataset source must contain one relation")
        tree = parsed[0]
        table = tree.args.get("from_").this
        extras = set(tree.args) - {
            "kind",
            "hint",
            "distinct",
            "expressions",
            "limit",
            "exclude",
            "operation_modifiers",
            "from_",
        }
        if (
            not isinstance(table, exp.Table)
            or not isinstance(table.this, exp.Identifier)
            or table.args.get("alias")
            or tree.args.get("limit")
            or any(tree.args.get(k) for k in extras)
        ):
            raise ValueError("dataset source must be a table or SELECT")
        return table.sql(dialect=kind)
    except (sqlglot.errors.SqlglotError, AttributeError) as exc:
        raise ValueError("invalid dataset source") from exc
