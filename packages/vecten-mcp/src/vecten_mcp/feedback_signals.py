"""Pure projections of durable, already-redacted audit and feedback records."""

from vecten_mcp.tool_service import evidence
from vecten_shared.osi.sql_policy import read_statement


def table_datasets(tables, model):
    if not model:
        return []

    def parts(value):
        return tuple(value.replace('"', "").casefold().split("."))

    index = getattr(model, "source_table_index", {})
    result = set()
    for table in tables:
        target = parts(table)
        candidates = [source for source in index if source[-len(target) :] == target]
        # A suffix is useful only when it identifies one physical source.
        if target in index:
            result.update(index[target])
        elif len(candidates) == 1:
            result.update(index[candidates[0]])
    return identifiers(list(result))


def identifiers(values):
    if not isinstance(values, list):
        return []
    return sorted({value[:256] for value in values[:100] if isinstance(value, str)})


def names(values):
    return sorted({value.casefold() for value in identifiers(values)})


def report_context(tool_calls, model=None):
    metrics, datasets, columns = [], [], []
    for call in tool_calls[:20]:
        arguments = call.get("arguments", {})
        if not isinstance(arguments, dict):
            continue
        metrics.extend(identifiers(arguments.get("metrics", [])))
        datasets.extend(identifiers(call.get("datasets", [])))
        dataset = arguments.get("dataset")
        if isinstance(dataset, str):
            datasets.append(dataset)
        columns.extend(identifiers(arguments.get("dimensions", [])))
        if model and isinstance(arguments.get("sql"), str):
            try:
                statement = read_statement(arguments["sql"], kind=model.kind)
            except ValueError:
                continue
            datasets.extend(table_datasets(statement.tables, model))
            columns.extend(statement.columns)
    if model:
        homes = model.homes
        datasets.extend(homes[metric] for metric in metrics if metric in homes)
    return {
        "names": names(metrics),
        "datasets": identifiers(datasets),
        "columns": identifiers(columns),
    }


def _base(row):
    return {
        "id": row["id"],
        "at": row["at"],
        "principal_id": row.get("principal_id"),
        "session_id": row.get("session_id"),
        "chain": row.get("request_chain_id"),
        "datasets": identifiers(row.get("datasets", [])),
        "columns": identifiers(row.get("columns", [])),
        "names": names(row.get("names", [])),
        "evidence": evidence(row),
        "category": row.get("category", ""),
        "reason": "",
        "fallback": False,
        "chained_sql": False,
    }


def audit_signal(row):
    if row.get("tool") not in {"query_metrics", "sql-exec"}:
        return None
    resolution = row.get("resolution")
    chained_sql = (
        row.get("tool") == "sql-exec"
        and row.get("request_chain_id")
        and row["request_chain_id"] != row["id"]
    )
    fallback = chained_sql and resolution == "agent_sql"
    if resolution == "refused":
        reason = row.get("refusal_reason", "unknown")
    elif resolution == "error" and row.get("warehouse_attempted"):
        reason = "warehouse_error"
    elif fallback:
        reason = ""
    else:
        return None
    signal = _base(row)
    arguments = row.get("arguments")
    context = row.get("refusal_context", {})
    if not isinstance(context, dict):
        context = {}
    missing = context.get("metric") or context.get("field") or context.get("definition")
    requested = arguments.get("metrics", []) if isinstance(arguments, dict) else []
    signal.update(
        reason=reason,
        fallback=bool(fallback),
        chained_sql=bool(chained_sql),
        names=names([missing] if isinstance(missing, str) else requested),
        datasets=identifiers([*signal["datasets"], *identifiers(context.get("datasets", []))]),
        evidence=evidence({**row, "agent_sql": row.get("sql") if chained_sql else ""}),
    )
    return signal


def report_signal(row):
    return _base(row)
