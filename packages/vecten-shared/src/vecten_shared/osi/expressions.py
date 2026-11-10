"""Expression safety shared by the server, CLI validator and compiler."""

import sqlglot
from sqlglot import exp

FUNCTIONS = frozenset(
    {
        "SUM",
        "COUNT",
        "AVG",
        "MIN",
        "MAX",
        "PERCENTILE_CONT",
        "PERCENTILE_DISC",
        "ABS",
        "ROUND",
        "FLOOR",
        "CEIL",
        "UPPER",
        "LOWER",
        "COALESCE",
        "NULLIF",
        "CONCAT",
        "LENGTH",
        "TRIM",
        "LTRIM",
        "RTRIM",
        "SUBSTRING",
        "DATE_TRUNC",
        "TIMESTAMP_TRUNC",
        "EXTRACT",
        "CAST",
        "TRY_CAST",
        "CASE",
        "IF",
        "COUNT_IF",
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "LAG",
        "LEAD",
    }
)


class DialectMissing(ValueError):
    def __init__(self, definition):
        super().__init__("dialect_missing")
        self.definition = definition


def parse_expression(text, dialect=None):
    trees = sqlglot.parse(text, read=dialect)
    if len(trees) != 1 or trees[0] is None:
        raise ValueError("an expression must contain exactly one expression")
    tree = trees[0]
    for node in tree.walk():
        if isinstance(node, (exp.Query, exp.Subquery, exp.DML, exp.DDL, exp.Command, exp.Into)):
            raise ValueError(f"expression cannot contain {type(node).__name__}")
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if name not in FUNCTIONS:
                raise ValueError(f"function {name} is outside the expression allow-list")
    return tree


def selected_expression(element, kind):
    by_dialect = {
        entry["dialect"]: entry["expression"] for entry in element["expression"]["dialects"]
    }
    if kind == "snowflake" and "SNOWFLAKE" in by_dialect:
        return parse_expression(by_dialect["SNOWFLAKE"], "snowflake")
    if "ANSI_SQL" not in by_dialect:
        raise DialectMissing(element.get("name"))
    return parse_expression(by_dialect["ANSI_SQL"])
