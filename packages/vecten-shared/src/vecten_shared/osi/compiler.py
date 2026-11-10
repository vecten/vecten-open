"""Single-dataset metric compilation with bound values and explicit refusals."""

import copy
import datetime

from sqlglot import exp

from vecten_shared.osi.expressions import DialectMissing, selected_expression
from vecten_shared.osi.extensions import read_vecten_extension
from vecten_shared.osi.validate import GRAINS

OPERATORS = {
    "=": "=",
    "equals": "=",
    "!=": "<>",
    "not equals": "<>",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
    "like": "LIKE",
}
TIME_KEYS = ("dimension", "end", "grain", "start")
FILTER_KEYS = ("field", "op", "value")
ORDER_KEYS = ("dir", "field")
ROWS_MAY = {
    "additive": ["filter", "sort", "sum_across_dimensions"],
    "non_additive": ["filter", "sort"],
    "semi_additive": ["filter", "sort", "sum_across_non_time"],
}


class Refusal(Exception):
    def __init__(self, reason, context=None):
        self.reason, self.context = reason, context or {}


def pyformat_statement(sql):
    """The compiled Postgres SQL with every `%` that is not a bound `%s` doubled.

    pyformat drivers read each `%` as placeholder syntax once values are bound, so a modulo,
    a LIKE pattern or a comment would break the statement. The compiler binds only `%s`
    outside quotes and comments, and sqlglot spaces the modulo operator, so an unquoted `%s`
    is always a placeholder.
    """
    out, i, end = [], 0, len(sql)
    while i < end:
        char = sql[i]
        if char in "'\"":
            close = i + 1
            while close < end:
                if sql[close] == char and sql.startswith(char * 2, close):
                    close += 2
                elif sql[close] == char:
                    break
                else:
                    close += 1
            out.append(sql[i : close + 1].replace("%", "%%"))
            i = close + 1
        elif sql.startswith("--", i) or sql.startswith("/*", i):
            close = sql.find("\n" if char == "-" else "*/", i)
            close = end if close < 0 else close + (1 if char == "-" else 2)
            out.append(sql[i:close].replace("%", "%%"))
            i = close
        elif sql.startswith("%s", i):
            out.append("%s")
            i += 2
        else:
            out.append("%%" if char == "%" else char)
            i += 1
    return "".join(out)


def only_keys(item, allowed, label):
    if set(item) - set(allowed):
        raise ValueError(f"{label} accepts only: {', '.join(allowed)}")


def time_bound(time, key):
    value = time[key]
    message = f"time.{key} must be an ISO date or timestamp, such as 2026-01-01"
    if not isinstance(value, str):
        raise ValueError(message)
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(message) from None
    return value


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


class Compiler:
    def __init__(self, model, denied, row_cap):
        self.model, self.requested_denied, self.row_cap = model, denied, row_cap
        self.denied = set()
        self.parameters, self.used_fields = [], {}
        self.home = None

    def compile(self, query):
        if not isinstance(query, dict):
            raise ValueError("query must be an object")
        try:
            try:
                hidden, _ = self.model.visibility(self.requested_denied)
                self.denied = {name.casefold() for name in hidden}
                return self._compile(query)
            except DialectMissing as missing:
                context = {"definition": missing.definition, "dialect": self.model.kind}
                raise Refusal("dialect_missing", context) from None
        except Refusal as refusal:
            skeleton = None
            if self.home:
                skeleton = (
                    "SELECT /* complete the requested calculation */ * FROM "
                    + self.relation(self.home)
                )
            if refusal.reason == "join_required":
                homes = refusal.context["datasets"]
                relationships = self.model.describe(denied=self.denied)["relationships"]
                relation = next(
                    (r for r in relationships if {r["from"], r["to"]} == set(homes)), None
                )
                if relation:
                    left, right = relation["from"], relation["to"]
                    conditions = [
                        f"{quoted(left)}.{quoted(a)} = {quoted(right)}.{quoted(b)}"
                        for a, b in zip(
                            relation["from_columns"], relation["to_columns"], strict=True
                        )
                    ]
                    skeleton = (
                        "SELECT /* complete the requested calculation */ * "
                        f"FROM {self.source(left)} "
                        f"AS {quoted(left)} JOIN {self.source(right)} AS {quoted(right)} ON "
                        + " AND ".join(conditions)
                    )
                    refusal.context["relationship"] = relation
            return {
                "resolution": "refused",
                "reason": refusal.reason,
                "context": refusal.context,
                "skeleton": skeleton,
                "sql": None,
                "parameters": [],
                "model": self.model.commit,
            }

    def source(self, name):
        from vecten_shared.osi.sql_policy import source_sql

        return source_sql(self.model.datasets[name]["source"], self.model.kind)

    def relation(self, name):
        source = self.source(name)
        # Postgres before 16 rejects a subquery in FROM without an alias.
        return f"{source} AS {quoted(name)}" if source.startswith("(") else source

    def field(self, value, *, dimension=False):
        if not isinstance(value, str):
            raise ValueError("field name must be a string")
        parts = value.split(".")
        home, name = parts if len(parts) == 2 else (self.home, value)
        if (home + "." + name).casefold() in self.denied:
            raise Refusal("unknown_field", {"field": value})
        fields = self.model.fields(home) if home in self.model.datasets else {}
        canonical = {key.casefold(): key for key in fields}
        name = canonical.get(name.casefold(), name)
        if name not in fields:
            alternatives = [
                dataset
                for dataset in self.model.datasets
                if name in self.model.fields(dataset)
                and (dataset + "." + name).casefold() not in self.denied
            ]
            if len(parts) == 1 and len(alternatives) == 1:
                raise Refusal("join_required", {"datasets": [self.home, alternatives[0]]})
            if len(parts) == 1 and alternatives:
                raise Refusal(
                    "ambiguous_dataset",
                    {"field": value, "home": self.home, "candidates": sorted(alternatives)},
                )
            raise Refusal("unknown_field", {"field": value})
        if home != self.home:
            raise Refusal("join_required", {"datasets": [self.home, home]})
        field = fields[name]
        if dimension and field["classification"] == "fact":
            raise Refusal("unknown_field", {"field": value})
        tree = selected_expression(field, self.model.kind)
        self.used_fields[home + "." + name] = {"expression": tree.sql(dialect=self.model.kind)}
        return name, tree

    def parameter(self, value):
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise ValueError("filter values must be scalar")
        self.parameters.append(value)
        return "%s" if self.model.kind == "postgres" else "?"

    def metric_expression(self, name):
        metric = self.model.metrics[name]
        if self.model.metric_columns(name) & self.denied:
            raise Refusal("unknown_field", {"metric": name})
        tree = selected_expression(metric, self.model.kind)
        if tree.find(exp.Window):
            raise Refusal("rollup_not_allowed", {"metric": name, "construct": "window"})

        def replace(node):
            if isinstance(node, exp.Column):
                other = self.model.homes.get(node.name) if node.name in self.model.metrics else None
                if other and other != self.home and node.name not in self.model.fields(self.home):
                    raise Refusal(
                        "join_required",
                        {"datasets": [self.home, other], "metric": name, "references": node.name},
                    )
                return self.field(node.name)[1]
            return node

        return tree.transform(replace).sql(dialect=self.model.kind)

    def _compile(self, query):
        names = query.get("metrics")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ValueError("metrics must be a nonempty list of names")
        if len(set(names)) != len(names):
            raise ValueError("metrics must be unique")
        for name in names:
            if name not in self.model.metrics:
                raise Refusal(
                    "unknown_metric",
                    {"metric": name, "suggestions": self.model.suggestions(name, self.denied)},
                )
        homes = {self.model.homes[n] for n in names}
        self.home = sorted(homes)[0]
        if len(homes) != 1:
            raise Refusal("join_required", {"datasets": sorted(homes)})
        expressions = {name: self.metric_expression(name) for name in names}
        dims = query.get("dimensions", [])
        if not isinstance(dims, list):
            raise ValueError("dimensions must be a list")
        groups = []
        for dimension in dims:
            name, tree = self.field(dimension, dimension=True)
            if name in {n for n, _ in groups} or name in names:
                raise ValueError("output names must be unique")
            groups.append((name, tree.sql(dialect=self.model.kind)))
        where, time_name, time_sql, grain = [], None, None, None
        time = query.get("time")
        if time is not None:
            if not isinstance(time, dict):
                raise ValueError("time must be an object")
            only_keys(time, TIME_KEYS, "time")
            grain = time.get("grain")
            if grain is not None and grain not in GRAINS:
                raise ValueError("time grain must be one of: " + ", ".join(GRAINS))
            if grain is None and "start" not in time and "end" not in time:
                raise ValueError("time needs a grain, a start or an end")
            time_name = time.get("dimension")
            if time_name is None:
                candidates = [
                    n
                    for n, f in self.model.fields(self.home).items()
                    if f["classification"] == "time_dimension"
                    and (self.home + "." + n).casefold() not in self.denied
                ]
                if len(candidates) != 1:
                    context = {"field": "time.dimension", "candidates": sorted(candidates)}
                    raise Refusal("unknown_field", context)
                time_name = candidates[0]
            time_name, tree = self.field(time_name, dimension=True)
            if self.model.fields(self.home)[time_name]["classification"] != "time_dimension":
                raise Refusal("unknown_field", {"field": time_name})
            time_sql = tree.sql(dialect=self.model.kind)
            if grain:
                if time_name in {n for n, _ in groups} or time_name in names:
                    raise ValueError("output names must be unique")
                groups.append((time_name, f"DATE_TRUNC('{grain}', {time_sql})"))
            for key, op in (("start", ">="), ("end", "<")):
                if key in time:
                    where.append(f"{time_sql} {op} {self.parameter(time_bound(time, key))}")
        filters = query.get("filters", [])
        if not isinstance(filters, list):
            raise ValueError("filters must be a list")
        for item in filters:
            if not isinstance(item, dict):
                raise ValueError("filter must be an object")
            only_keys(item, FILTER_KEYS, "filter")
            _, tree = self.field(item.get("field"))
            field_sql = tree.sql(dialect=self.model.kind)
            op, value = item.get("op"), item.get("value")
            if not isinstance(op, str):
                raise ValueError("filter operator must be a string")
            if op in OPERATORS:
                where.append(f"{field_sql} {OPERATORS[op]} {self.parameter(value)}")
            elif op in {"in", "between"}:
                if not isinstance(value, list):
                    raise ValueError(f"{op} needs a list of values")
                if op == "between" and len(value) != 2:
                    raise ValueError("between needs two values")
                values = [self.parameter(v) for v in value]
                where.append(
                    ("FALSE" if not values else f"{field_sql} IN (" + ", ".join(values) + ")")
                    if op == "in"
                    else f"{field_sql} BETWEEN {values[0]} AND {values[1]}"
                )
            elif op == "is null":
                where.append(f"{field_sql} IS " + ("NOT NULL" if value is False else "NULL"))
            else:
                raise ValueError(
                    "filter operator must be one of: "
                    + ", ".join((*OPERATORS, "in", "between", "is null"))
                )
        suffix = " FROM " + self.relation(self.home)
        if where:
            suffix += " WHERE " + " AND ".join("(" + term + ")" for term in where)

        def select(grouping, metrics):
            columns = [f"{value} AS {quoted(name)}" for name, value in grouping]
            columns += [f"{value} AS {quoted(name)}" for name, value in metrics.items()]
            return (
                "SELECT "
                + ", ".join(columns)
                + suffix
                + (" GROUP BY " + ", ".join(value for _, value in grouping) if grouping else "")
            )

        sql = select(groups, expressions)
        semi = {
            n: read_vecten_extension(self.model.metrics[n]) or {}
            for n in names
            if self.model.metrics[n]["additivity"] == "semi_additive"
        }
        if semi:
            base_grain = (read_vecten_extension(self.model.datasets[self.home]) or {}).get("grain")
            if not grain or base_grain not in GRAINS:
                raise Refusal(
                    "rollup_not_allowed",
                    {
                        "metrics": list(semi),
                        "dimension": "time",
                        "requires": "dataset grain and requested time grain",
                    },
                )
            if GRAINS.index(grain) > GRAINS.index(base_grain):
                for name, rule in semi.items():
                    if rule.get("rollup") not in {"last", "first"} or rule.get(
                        "non_additive_over", "time"
                    ) not in {"time", time_name}:
                        raise Refusal(
                            "rollup_not_allowed",
                            {"metric": name, "dimension": time_name, "rules": ["last", "first"]},
                        )
                base_groups = [
                    (n, value if n != time_name else f"DATE_TRUNC('{base_grain}', {time_sql})")
                    for n, value in groups
                ]
                base = select(base_groups, {n: expressions[n] for n in semi})
                partition = ", ".join(
                    quoted(n) if n != time_name else f"DATE_TRUNC('{grain}', {quoted(n)})"
                    for n, _ in groups
                )
                ranks = ", ".join(
                    f"ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {quoted(time_name)} "
                    f"{'DESC' if rule['rollup'] == 'last' else 'ASC'}) AS {quoted('_rank_' + n)}"
                    for n, rule in semi.items()
                )
                aggregates = ", ".join(
                    f"MAX(CASE WHEN {quoted('_rank_' + n)} = 1 THEN {quoted(n)} END) AS {quoted(n)}"
                    for n in semi
                )
                group_columns = ", ".join(
                    (quoted(n) if n != time_name else f"DATE_TRUNC('{grain}', {quoted(n)})")
                    + " AS "
                    + quoted(n)
                    for n, _ in groups
                )
                rollup = f"SELECT {group_columns}, {aggregates} FROM _ranked GROUP BY {partition}"
                output = [f"n.{quoted(n)} AS {quoted(n)}" for n, _ in groups]
                output += [f"{'r' if n in semi else 'n'}.{quoted(n)} AS {quoted(n)}" for n in names]
                join = " AND ".join(
                    f"n.{quoted(n)} IS NOT DISTINCT FROM r.{quoted(n)}" for n, _ in groups
                )
                sql = (
                    f"WITH _normal AS ({sql}), _base AS ({base}), "
                    f"_ranked AS (SELECT *, {ranks} FROM _base), _rollup AS ({rollup}) SELECT "
                    + ", ".join(output)
                    + f" FROM _normal n JOIN _rollup r ON {join}"
                )
                self.parameters *= 2
        order = query.get("order_by", [])
        if not isinstance(order, list):
            raise ValueError("order_by must be a list")
        ordering = []
        for item in order:
            if isinstance(item, dict):
                only_keys(item, ORDER_KEYS, "order_by")
            if not isinstance(item, dict) or item.get("field") not in names + [
                n for n, _ in groups
            ]:
                raise Refusal(
                    "unknown_field",
                    {"field": item.get("field") if isinstance(item, dict) else None},
                )
            direction = item.get("dir", "asc")
            if not isinstance(direction, str) or direction not in {"asc", "desc"}:
                raise ValueError("order direction must be asc or desc")
            ordering.append(quoted(item["field"]) + " " + direction.upper())
        if ordering:
            sql += " ORDER BY " + ", ".join(ordering)
        limit = query.get("limit", self.row_cap)
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        sql += " LIMIT " + str(min(limit, self.row_cap))
        return {
            "resolution": "compiled",
            "sql": sql,
            "parameters": self.parameters,
            "model": self.model.commit,
            "datasets": [self.home],
            "definitions": {
                "source": self.model.datasets[self.home]["source"],
                "fields": self.used_fields,
                "metrics": {
                    n: {
                        "expression": expressions[n],
                        "verified": copy.deepcopy(self.model.metrics[n]["verified"]),
                    }
                    for n in names
                },
            },
            "operations": {
                n: {
                    "additivity": self.model.metrics[n]["additivity"],
                    "rows_may": ROWS_MAY[self.model.metrics[n]["additivity"]],
                }
                for n in names
            },
            "further_aggregation": "Call query_metrics at a coarser grain.",
        }
