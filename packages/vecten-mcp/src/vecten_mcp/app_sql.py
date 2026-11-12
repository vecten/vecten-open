"""The relations an app principal's sql-exec statement may read: its declared datasets' tables.

The app principal's warehouse roles are those of its groups, which usually read more than the
app declares, so this check, made before the warehouse, is the boundary. It fails closed: a
name that might be a table is a table, and anything that could reach a relation other than
through a plain table reference (a function off the warehouse's list, a table function, a cast
to a relation's row type, a stage) is refused.
"""

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from vecten_shared.osi.sql_policy import read_statement


def _functions(*names):
    return frozenset(getattr(exp, name) for name in names)


# The functions an app may call, by SQLGlot class, per warehouse. A class SQLGlot knows is not
# enough: it is written back under a name the warehouse may resolve as a function some schema
# defines, so only these reporting built-ins pass. Each also holds no relation name.
_COMMON = """
Abs ApproxDistinct ArrayAgg ArraySize Ascii Avg Case Cast Ceil Chr Coalesce Concat Count
CurrentDate CurrentTimestamp CurrentUser DateAdd DenseRank Exists Exp Explode Extract
FirstValue Floor
Greatest GroupConcat If Initcap JSONExtract JSONExtractScalar Lag LastValue Lead Least Left
Length Ln Log LogicalAnd LogicalOr Lower MD5 Max Median Min Ntile Nullif Pad PercentileCont
PercentileDisc Pow Rank RegexpReplace Repeat Replace Reverse Right Round RowNumber Sign
SplitPart Sqrt Stddev StrPosition Substring Sum TimestampTrunc Trim TryCast Unnest Upper
Variance
""".split()
FUNCTIONS = {
    "postgres": _functions(
        *_COMMON,
        *"""
ExplodingGenerateSeries StrToDate TimeToStr
""".split(),
    ),
    "snowflake": _functions(
        *_COMMON,
        *"""
AnyValue ApproxQuantile Array ArrayContains Contains ConvertTimezone CountIf DateDiff
DateFromParts Day DayOfWeek EndsWith GenerateSeries GetExtract LastDay Month Nvl2 ParseJSON
Quarter RegexpExtract Split StartsWith Struct ToChar ToNumber TsOrDsToDate Uuid Week Year
""".split(),
    ),
}
# What may stand where a query reads rows from; a function there is a table function.
SOURCES = (exp.Table, exp.Subquery, exp.Values, exp.Unnest, exp.Lateral)
# What may follow LATERAL: a subquery, or a function that makes rows from a column's value.
LATERAL_SOURCES = (exp.Subquery, exp.Unnest, exp.Explode, exp.ExplodingGenerateSeries)
REFUSAL = "an app reads only tables and subqueries, not table functions or stages"


def relation(source, kind):
    """A table source's identifier parts as the warehouse resolves them."""
    tree = normalize_identifiers(
        sqlglot.parse_one("SELECT * FROM " + source, read=kind), dialect=kind
    )
    table = tree.args["from_"].this
    if not isinstance(table, exp.Table) or not all(
        isinstance(part, exp.Identifier) for part in table.parts
    ):
        raise ValueError("a dataset source must be a table to be read by sql-exec")
    return tuple(part.name for part in table.parts)


def _ctes(with_):
    return [cte.alias for cte in with_.expressions]


def _visible_ctes(node):
    """CTE names an unqualified table name at `node` refers to: those of every enclosing WITH
    whose body holds it, and inside a CTE's definition only the CTEs before it (and itself
    under RECURSIVE). A later CTE is not visible, so its name there is a table."""
    names = set()
    child, parent = node, node.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            ctes = parent.expressions
            index = next((i for i, cte in enumerate(ctes) if cte is child), None)
            if index is not None:
                names.update(_ctes(parent)[:index])
                if parent.args.get("recursive"):
                    names.add(ctes[index].alias)
        elif isinstance(parent, exp.Query):
            with_ = parent.args.get("with_")
            if with_ is not None and child is not with_:
                names.update(_ctes(with_))
        child, parent = parent, parent.parent
    return names


def _display(table, kind):
    table = table.copy()
    table.set("alias", None)
    return table.sql(dialect=kind)


def _tree_problem(tree, kind, relations):
    tree = normalize_identifiers(tree.copy(), dialect=kind)
    for node in tree.walk():
        if isinstance(node, exp.Operator):
            return "an OPERATOR() call is not available to an app"
        # AND and OR are connectors SQLGlot models as functions; they call nothing.
        if (
            isinstance(node, exp.Func)
            and not isinstance(node, exp.Connector)
            and type(node) not in FUNCTIONS[kind]
        ):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            return f"the function {name} is not available to an app"
        if isinstance(node, exp.Lateral) and not isinstance(node.this, LATERAL_SOURCES):
            return REFUSAL
        if isinstance(node, exp.DataType) and (
            node.this == exp.DataType.Type.USERDEFINED
            or str(getattr(node.this, "value", node.this)).upper().startswith("REG")
        ):
            return f"a cast to {node.sql(dialect=kind)} is not available to an app"
        if isinstance(node, (exp.From, exp.Join)) and not isinstance(node.this, SOURCES):
            return REFUSAL
        if not isinstance(node, exp.Table):
            continue
        if node.args.get("rows_from") or not all(
            isinstance(part, exp.Identifier) for part in node.parts
        ):
            return REFUSAL
        parts = tuple(part.name for part in node.parts)
        if len(parts) == 1 and parts[0] in _visible_ctes(node):
            continue
        if parts not in relations:
            return f"{_display(node, kind)} is not the table of one of the app's declared datasets"
    return None


def sql_problem(text, *, kind, relations):
    """Why an app principal may not run `text`, or None; `relations` holds the parts of each
    declared dataset's table as `relation` gives them."""
    try:
        tokens = sqlglot.tokenize(text, read=kind) if isinstance(text, str) else []
    except sqlglot.errors.SqlglotError:
        return "SQL could not be parsed as one read statement"
    if any(token.comments for token in tokens):
        return "comments are not allowed in an app's SQL"
    if not tokens or tokens[0].text.upper() not in {"SELECT", "WITH"}:
        return "an app's SQL must be one SELECT or WITH query"
    try:
        statement = read_statement(text, kind=kind)
        problem = _tree_problem(statement.tree, kind, relations)
        if problem:
            return problem
        # The warehouse receives the statement as SQLGlot writes it back, so that text is
        # checked as well as the one the app sent.
        return _tree_problem(read_statement(statement.sql, kind=kind).tree, kind, relations)
    except ValueError as exc:
        return str(exc)
    except Exception:
        # Whatever the parser cannot read is refused, never passed on as a tool failure.
        return "SQL could not be parsed as one read statement"
