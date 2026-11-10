"""Usage analytics computed from `mcp_state` alone; the warehouse is never queried."""

import time
from collections import Counter
from datetime import UTC, date, datetime

VIEWS = (
    "questions",
    "top",
    "unused_fields",
    "undescribed_fields",
    "refusals",
    "open_gaps",
    "field_usage",
)
QUESTION_TOOLS = ("query_metrics", "sql-exec")
OPEN_STATUSES = ("open", "triaged")
DEFAULT_DAYS = 30
MAX_DAYS = 3650
TOP = 20
RECENT = 50


def day(at):
    return datetime.fromtimestamp(at, UTC).date().isoformat()


def listed(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def bare(column):
    return column.rsplit(".", 1)[-1].replace('"', "").casefold()


def model_fields(model):
    if model is None:
        return []
    return sorted(
        (dataset, field["name"])
        for dataset, definition in model.datasets.items()
        for field in definition.get("fields", [])
    )


def field_counts(rows, model):
    """A field counts once per row whose column list names it.

    A row that recorded datasets matches only fields of those datasets; a row without any
    matches by bare column name in every dataset, because its SQL never resolved a source.
    """
    if model is None:
        return Counter()
    references = model.field_references
    counts = Counter()
    for row in rows:
        names = {bare(column) for column in listed(row.get("columns"))}
        if not names:
            continue
        recorded = [d for d in listed(row.get("datasets")) if d in references]
        if listed(row.get("datasets")) and not recorded:
            continue
        for dataset in recorded or references:
            for field in model.datasets[dataset].get("fields", []):
                key = field["name"].casefold()
                if names & (references[dataset].get(key, frozenset()) | {key}):
                    counts[(dataset, field["name"])] += 1
    return counts


def latest_first(text):
    return -date.fromisoformat(text).toordinal()


def questions(rows, principals):
    asked = [row for row in rows if row.get("tool") in QUESTION_TOOLS]
    per_person = Counter((day(row["at"]), row.get("principal_id")) for row in asked)
    per_day = Counter(day(row["at"]) for row in asked)
    return {
        "rows": sorted(
            (
                {
                    "day": d,
                    "principal_id": pid,
                    "name": principals.get(pid, pid or "unknown"),
                    "questions": count,
                }
                for (d, pid), count in per_person.items()
            ),
            key=lambda r: (latest_first(r["day"]), -r["questions"], r["name"]),
        ),
        "by_day": [
            {"day": d, "questions": count}
            for d, count in sorted(per_day.items(), key=lambda item: latest_first(item[0]))
        ],
        "total": len(asked),
    }


def ranked(counter):
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:TOP]
    ]


def top(rows):
    """Answered metrics and the datasets that answered questions, never refused requests."""
    metrics, datasets = Counter(), Counter()
    for row in rows:
        if row.get("tool") not in QUESTION_TOOLS or row.get("resolution") == "refused":
            continue
        if row.get("tool") == "query_metrics" and row.get("resolution") == "compiled":
            metrics.update(set(listed(row.get("metrics"))))
        datasets.update(set(listed(row.get("datasets"))))
    return {"metrics": ranked(metrics), "datasets": ranked(datasets)}


def refusals(rows, principals, redact):
    refused = [row for row in rows if row.get("resolution") == "refused"]
    per_day = Counter((day(row["at"]), row.get("refusal_reason") or "unknown") for row in refused)
    recent = sorted(refused, key=lambda row: row["at"], reverse=True)[:RECENT]
    return {
        "by_day": [
            {"day": d, "reason": reason, "count": count}
            for (d, reason), count in sorted(
                per_day.items(), key=lambda item: (latest_first(item[0][0]), -item[1], item[0][1])
            )
        ],
        "recent": [
            {
                "id": row.get("id"),
                "at": row["at"],
                "reason": row.get("refusal_reason") or "unknown",
                "principal_id": row.get("principal_id"),
                "name": principals.get(row.get("principal_id"), row.get("principal_id")),
                "question": "[redacted]" if redact else (row.get("question") or ""),
            }
            for row in recent
        ],
    }


def analytics_view(name, *, rows, model, gaps, principals, redact, gap_view):
    if name not in VIEWS:
        raise ValueError("view must be one of: " + ", ".join(VIEWS))
    if name == "questions":
        return questions(rows, principals)
    if name == "top":
        return top(rows)
    if name == "refusals":
        return refusals(rows, principals, redact)
    if name == "open_gaps":
        selected = [(key, row) for key, row in gaps if row.get("status", "open") in OPEN_STATUSES]
        selected.sort(key=lambda item: item[1].get("last_seen") or 0, reverse=True)
        return {
            "gaps": [
                {k: v for k, v in gap_view(key, row).items() if k != "evidence"}
                for key, row in selected
            ]
        }
    fields = model_fields(model)
    if name == "undescribed_fields":
        return {
            "fields": [
                {"dataset": dataset, "field": field["name"]}
                for dataset, field in _named(model, fields)
                if not str(field.get("description") or "").strip()
            ]
        }
    counts = field_counts(rows, model)
    if name == "unused_fields":
        return {
            "fields": [
                {"dataset": dataset, "field": field["name"]}
                for dataset, field in _named(model, fields)
                if not counts[(dataset, field["name"])]
            ]
        }
    metrics = Counter()
    for row in rows:
        if row.get("tool") == "query_metrics" and row.get("resolution") == "compiled":
            metrics.update(set(listed(row.get("metrics"))))
    return {
        "fields": [
            {"dataset": dataset, "field": field["name"], "count": counts[(dataset, field["name"])]}
            for dataset, field in _named(model, fields)
        ],
        "metrics": [
            {"name": metric, "count": metrics[metric]}
            for metric in sorted(model.metrics if model else [])
        ],
    }


def _named(model, pairs):
    return [(dataset, model.fields(dataset)[name]) for dataset, name in pairs]


def window(days, default):
    days = default if days is None else days
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be an integer from 1 to {MAX_DAYS}")
    return days


class Analytics:
    def __init__(self, runtime):
        from vecten_mcp.tool_service import ToolService

        self.runtime, self.state = runtime, runtime.state
        self.tools = getattr(runtime, "tool_service", None) or ToolService(runtime)

    def view(self, name, days=None, *, now=None):
        if name not in VIEWS:
            raise ValueError("view must be one of: " + ", ".join(VIEWS))
        default = DEFAULT_DAYS
        if name == "unused_fields":
            setting = self.state.get("settings", "audit_retention_days") or {}
            default = setting.get("value", 90)
        days = window(days, default)
        until = time.time() if now is None else now
        since = until - days * 86400
        model = getattr(self.runtime, "model", None)
        result = analytics_view(
            name,
            rows=self.runtime.audit.projection(since=since, until=until),
            model=model.current if model else None,
            gaps=self.state.scan("gaps"),
            principals={p["id"]: p["name"] for p in self.state.principals()},
            redact=self.tools.redacted(),
            gap_view=self.tools.gap_view,
        )
        return {"view": name, "days": days, "since": since, "until": until, **result}
