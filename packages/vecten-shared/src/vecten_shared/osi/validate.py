"""The validator `vecten mcp validate` and the MCP server share: one verdict on osi/.

Source existence needs a warehouse connection, so this module never checks it.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import sqlglot
from jsonschema import Draft202012Validator
from sqlglot import exp
from sqlglot.errors import SqlglotError

from vecten_shared.osi import OSI_VERSION, load_schema
from vecten_shared.osi.documents import Document, Model, list_documents, load_document, merge
from vecten_shared.osi.extensions import read_vecten_extension
from vecten_shared.osi.issues import Issue, ModelError

DIALECT_FOR: dict[str, tuple[str, ...]] = {
    "postgres": ("ANSI_SQL",),
    "snowflake": ("SNOWFLAKE", "ANSI_SQL"),
}
READ_DIALECT: dict[str, str | None] = {"ANSI_SQL": None, "SNOWFLAKE": "snowflake"}
SOURCE_SKIPPED = "source existence skipped: no warehouse connection"
NO_DATASETS = "no datasets yet"
PIN_HINT = 'name its home with the Vecten extension {"dataset": "<name>"}'
SEMI_ADDITIVE = {"additivity": "semi_additive", "non_additive_over": "time", "rollup": "last"}
ADDITIVITY = ("additive", "non_additive", "semi_additive")
ROLLUPS = ("last", "first")
GRAINS = ("day", "week", "month", "quarter", "year")


@dataclass
class Report:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    homes: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_data(self) -> dict:
        return {
            "ok": self.ok,
            "errors": [asdict(i) for i in self.errors],
            "warnings": [asdict(i) for i in self.warnings],
            "notes": list(self.notes),
            "homes": dict(self.homes),
        }


def _chosen(element: dict, warehouse_kind: str) -> tuple[str, str] | None:
    entries = (element.get("expression") or {}).get("dialects") or []
    by_dialect = {e.get("dialect"): e.get("expression") for e in entries if isinstance(e, dict)}
    for dialect in DIALECT_FOR[warehouse_kind]:
        if isinstance(by_dialect.get(dialect), str):
            return dialect, by_dialect[dialect]
    return None


def expression_for(element: dict, warehouse_kind: str) -> str | None:
    chosen = _chosen(element, warehouse_kind)
    return chosen[1] if chosen else None


def _parse(expression: str, dialect: str) -> exp.Expression:
    try:
        tree = sqlglot.parse_one(expression, read=READ_DIALECT[dialect])
    except SqlglotError as exc:
        raise ValueError(f"cannot parse {expression!r} as {dialect}: {exc}") from None
    if tree is None:
        raise ValueError(f"cannot parse {expression!r} as {dialect}: empty expression")
    return tree


def _references(tree: exp.Expression) -> set[tuple[str | None, str]]:
    return {(col.table.lower() or None, col.name.lower()) for col in tree.find_all(exp.Column)}


def column_references(expression: str, dialect: str) -> set[tuple[str | None, str]]:
    return _references(_parse(expression, dialect))


def column_names(expression: str, dialect: str) -> set[str]:
    return {name for _, name in column_references(expression, dialect)}


def is_fresh(osi_dir: Path, generated_text: str) -> bool:
    path = osi_dir / "datasets.json"
    return path.is_file() and path.read_bytes() == generated_text.encode("utf-8")


def _schema_issues(
    validator: Draft202012Validator, data: dict, document: str | None
) -> list[Issue]:
    errors = sorted(validator.iter_errors(data), key=lambda e: e.json_path)
    return [
        Issue("schema", e.message, document, "/".join(str(p) for p in e.absolute_path))
        for e in errors
    ]


def _per_document_schema() -> dict:
    # A project splits its model across documents, so one document may hold no dataset;
    # the merged model still has to satisfy the untouched minItems.
    schema = copy.deepcopy(load_schema())
    schema["$defs"]["SemanticModel"]["properties"]["datasets"].pop("minItems", None)
    return schema


def _load(osi_dir: Path, report: Report) -> list[Document]:
    validator = Draft202012Validator(_per_document_schema())
    documents: list[Document] = []
    for path in list_documents(osi_dir):
        name = (
            path.relative_to(osi_dir).as_posix()
            if path.is_relative_to(osi_dir)
            else path.as_posix()
        )
        try:
            doc = load_document(path)
        except ModelError as exc:
            report.errors += [replace(i, document=name) for i in exc.issues]
            continue
        issues = _schema_issues(validator, doc.data, name)
        if issues:
            report.errors += issues
            continue
        documents.append(Document(Path(name), doc.data))
    return documents


def _extension_problems(kind: str, data: dict) -> list[str]:
    problems = []
    if kind == "metric":
        if "dataset" in data and not isinstance(data["dataset"], str):
            problems.append("dataset must be a dataset name")
        if "additivity" in data and data["additivity"] not in ADDITIVITY:
            problems.append(f"additivity must be one of {', '.join(ADDITIVITY)}")
        if "rollup" in data and data["rollup"] not in ROLLUPS:
            problems.append(f"rollup must be one of {', '.join(ROLLUPS)}")
        if "non_additive_over" in data and data["non_additive_over"] != "time":
            problems.append("non_additive_over must be time")
    else:
        if "snapshot_derived" in data and not isinstance(data["snapshot_derived"], bool):
            problems.append("snapshot_derived must be true or false")
        if "grain" in data and data["grain"] not in GRAINS:
            problems.append(f"grain must be one of {', '.join(GRAINS)}")
    return problems


def _extension(element: dict, kind: str, label: str, document: str, report: Report) -> dict:
    """The element's Vecten extension, or {} once a bad value has been reported."""
    try:
        data = read_vecten_extension(element) or {}
    except ValueError as exc:
        report.errors.append(Issue("invalid_extension", f"{label}: {exc}", document))
        return {}
    problems = _extension_problems(kind, data)
    report.errors += [Issue("invalid_extension", f"{label}: {p}", document) for p in problems]
    return {} if problems else data


def _check_fields(model: Model, report: Report) -> None:
    for ds_name, (dataset, path) in model.datasets.items():
        seen: set[str] = set()
        for f in dataset.get("fields") or []:
            name = str(f.get("name", "")).lower()
            if name in seen:
                report.errors.append(
                    Issue(
                        "duplicate_field",
                        f"dataset {ds_name} defines field {name} twice",
                        path.as_posix(),
                    )
                )
            seen.add(name)
        keys = [list(dataset.get("primary_key") or [])]
        keys += [list(k) for k in dataset.get("unique_keys") or []]
        missing = sorted({c for key in keys for c in key if c.lower() not in seen})
        if missing:
            report.errors.append(
                Issue(
                    "unknown_key_field",
                    f"dataset {ds_name}: key column {', '.join(missing)} is not a field",
                    path.as_posix(),
                )
            )


def _check_dialects(model: Model, warehouse_kind: str, report: Report) -> None:
    wanted = " or ".join(DIALECT_FOR[warehouse_kind])
    for ds_name, (dataset, path) in model.datasets.items():
        for f in dataset.get("fields") or []:
            if _chosen(f, warehouse_kind) is None:
                report.errors.append(
                    Issue(
                        "dialect_missing",
                        f"field {ds_name}.{f.get('name')} has no {wanted} expression",
                        path.as_posix(),
                    )
                )
    for name, (metric, path) in model.metrics.items():
        if _chosen(metric, warehouse_kind) is None:
            report.errors.append(
                Issue(
                    "dialect_missing", f"metric {name} has no {wanted} expression", path.as_posix()
                )
            )


def _check_relationships(model: Model, fields: dict[str, set[str]], report: Report) -> None:
    for name, (rel, path) in model.relationships.items():
        for end, columns in (("from", "from_columns"), ("to", "to_columns")):
            dataset = rel.get(end)
            if dataset not in fields:
                report.errors.append(
                    Issue(
                        "unknown_dataset",
                        f"relationship {name}: {end} names {dataset!r}, which no document defines",
                        path.as_posix(),
                    )
                )
                continue
            missing = sorted({c.lower() for c in rel.get(columns) or []} - fields[dataset])
            if missing:
                report.errors.append(
                    Issue(
                        "unknown_field",
                        f"relationship {name}: {dataset} has no field {', '.join(missing)}",
                        path.as_posix(),
                    )
                )


def _home(
    name: str,
    refs: set[tuple[str | None, str]],
    override: object,
    fields: dict[str, set[str]],
) -> tuple[str | None, list[tuple[str, str]]]:
    """The metric's home dataset, or None with (code, message) problems.

    The v0.1 compiler reads one dataset per metric, so every qualifier must name the home.
    """
    by_lower = {ds.lower(): ds for ds in fields}
    qualifiers = sorted({q for q, _ in refs if q is not None})
    unknown = [q for q in qualifiers if q not in by_lower]
    if unknown:
        return None, [
            ("unknown_dataset", f"metric {name}: no dataset {', '.join(map(repr, unknown))}")
        ]
    home, problems = _candidate(name, refs, override, fields, [by_lower[q] for q in qualifiers])
    if home is None:
        return None, problems
    source = " from the override" if override is not None else ""
    for qualifier, column in sorted(refs, key=lambda r: (r[0] or "", r[1])):
        owner = home if qualifier is None else by_lower[qualifier]
        if owner != home:
            problems.append(
                (
                    "ambiguous_dataset",
                    f"metric {name} has home {home}{source} but qualifies {column} with {owner}; "
                    "a metric reads one dataset",
                )
            )
        elif column not in fields[home]:
            problems.append(("unknown_field", f"metric {name}: {home} has no field {column}"))
    return (None if problems else home), problems


def _candidate(
    name: str,
    refs: set[tuple[str | None, str]],
    override: object,
    fields: dict[str, set[str]],
    qualified: list[str],
) -> tuple[str | None, list[tuple[str, str]]]:
    if override is not None:
        if not isinstance(override, str) or override not in fields:
            return None, [("unknown_dataset", f"metric {name}: the override names {override!r}")]
        return override, []
    if refs and all(q is not None for q, _ in refs):
        if len(qualified) > 1:
            named = ", ".join(qualified)
            return None, [("ambiguous_dataset", f"metric {name} spans {named}; {PIN_HINT}")]
        return qualified[0], []
    columns = {c for _, c in refs}
    covering = sorted(ds for ds, names in fields.items() if columns <= names)
    if not covering:
        known = set().union(*fields.values()) if fields else set()
        missing = sorted(columns - known)
        if not missing:
            together = ", ".join(sorted(columns))
            return None, [("unknown_field", f"metric {name}: no one dataset has {together}")]
        return None, [
            ("unknown_field", f"metric {name}: no dataset has field {', '.join(missing)}")
        ]
    if len(covering) > 1:
        return None, [
            (
                "ambiguous_dataset",
                f"metric {name} fits {', '.join(covering)}; {PIN_HINT}",
            )
        ]
    return covering[0], []


def _warnings(
    name: str, tree: exp.Expression, snapshot: bool, home: str, document: str
) -> list[Issue]:
    found: list[Issue] = []
    aggregates = list(tree.find_all(exp.AggFunc))
    if snapshot and aggregates and all(isinstance(a, exp.Sum) for a in aggregates):
        found.append(
            Issue(
                "semi_additive_candidate",
                f"metric {name} sums over {home}, a snapshot-derived dataset; where a row repeats "
                f"per snapshot date, a sum across dates counts it again. Add the Vecten extension "
                f"{json.dumps(SEMI_ADDITIVE)}, or declare its additivity if it is additive.",
                document,
                severity="warning",
            )
        )
    risky = (exp.Distinct, exp.Avg, exp.Div, exp.Window)
    if any(True for _ in tree.find_all(*risky)):
        found.append(
            Issue(
                "additivity_undeclared",
                f"metric {name} uses DISTINCT, AVG, a division or a window function and "
                "declares no additivity; a rollup of it is not a sum of its parts",
                document,
                severity="warning",
            )
        )
    return found


def _check_metrics(
    model: Model, fields: dict[str, set[str]], warehouse_kind: str, report: Report
) -> None:
    snapshot = {
        ds: bool(
            _extension(dataset, "dataset", f"dataset {ds}", path.as_posix(), report).get(
                "snapshot_derived"
            )
        )
        for ds, (dataset, path) in model.datasets.items()
    }
    for name, (metric, path) in model.metrics.items():
        document = path.as_posix()
        chosen = _chosen(metric, warehouse_kind)
        if chosen is None:
            continue
        dialect, expression = chosen
        try:
            tree = _parse(expression, dialect)
        except ValueError as exc:
            report.errors.append(Issue("expression_invalid", f"metric {name}: {exc}", document))
            continue
        refs = _references(tree)
        extension = _extension(metric, "metric", f"metric {name}", document, report)
        home, problems = _home(name, refs, extension.get("dataset"), fields)
        report.errors += [Issue(code, message, document) for code, message in problems]
        if home is None:
            continue
        report.homes[name] = home
        if "additivity" not in extension:
            report.warnings += _warnings(name, tree, snapshot[home], home, document)


def validate_osi(osi_dir: Path, *, warehouse_kind: str) -> Report:
    report = Report(notes=[SOURCE_SKIPPED])
    if not osi_dir.is_dir():
        message = f"no Ossie directory at {osi_dir}; every project keeps its model under osi/"
        report.errors.append(Issue("missing_osi_dir", message))
        return report
    documents = _load(osi_dir, report)
    try:
        model = merge(documents)
    except ModelError as exc:
        report.errors += exc.issues
        return report
    if report.errors:
        return report
    if not model.datasets:
        report.notes.append(NO_DATASETS)
    else:
        merged = {
            "version": OSI_VERSION,
            "semantic_model": [
                {
                    "name": "merged",
                    "datasets": [e for e, _ in model.datasets.values()],
                    "metrics": [e for e, _ in model.metrics.values()],
                    "relationships": [e for e, _ in model.relationships.values()],
                }
            ],
        }
        report.errors += _schema_issues(Draft202012Validator(load_schema()), merged, None)
        if report.errors:
            return report
    _check_dialects(model, warehouse_kind, report)
    _check_fields(model, report)
    fields = {
        ds: {str(f.get("name", "")).lower() for f in dataset.get("fields") or []}
        for ds, (dataset, _) in model.datasets.items()
    }
    _check_relationships(model, fields, report)
    from vecten_shared.osi.expressions import selected_expression

    expressions = [
        (f"field {name}.{field['name']}", field, path)
        for name, (dataset, path) in model.datasets.items()
        for field in dataset.get("fields", [])
    ]
    expressions += [
        (f"metric {name}", metric, path) for name, (metric, path) in model.metrics.items()
    ]
    for label, element, path in expressions:
        try:
            selected_expression(element, warehouse_kind)
        except (ValueError, SqlglotError) as exc:
            report.errors.append(Issue("expression_invalid", f"{label}: {exc}", str(path)))
    from vecten_shared.osi.sql_policy import source_sql

    for name, (dataset, path) in model.datasets.items():
        try:
            source_sql(dataset["source"], warehouse_kind)
        except ValueError as exc:
            report.errors.append(Issue("source_invalid", f"dataset {name}: {exc}", str(path)))
    _check_metrics(model, fields, warehouse_kind, report)
    return report
