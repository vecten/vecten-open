"""The datasets document, generated from the manifest and catalog of a green dbt build.

One dataset per mart, ordered by alias, with fields in declared column order, so the same
project renders the same bytes on every warehouse.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from vecten_shared.osi import OSI_VERSION
from vecten_shared.osi.extensions import vecten_extension
from vecten_shared.osi.issues import Issue

MARTS_SCHEMA = "marts"
TIME_SUFFIXES = ("_at", "_date")
KEY_SUFFIX = "_id"
NUMERIC_TYPES = frozenset(
    {
        "number",
        "numeric",
        "decimal",
        "int",
        "integer",
        "bigint",
        "smallint",
        "tinyint",
        "float",
        "float4",
        "float8",
        "double",
        "real",
        "double precision",
    }
)
TEMPORAL_TYPES = frozenset(
    {
        "date",
        "time",
        "timestamp",
        "timestamptz",
        "timestamp_ntz",
        "timestamp_ltz",
        "timestamp_tz",
        "timestamp without time zone",
        "timestamp with time zone",
        "datetime",
    }
)
_LEADING_WORD = re.compile(r"[a-z0-9_]+")


class GeneratorError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("\n".join(i.render() for i in issues))


def _type_names(catalog_type: str) -> set[str]:
    """The type with its parameters removed, and its leading word.

    Snowflake reports `NUMBER(38,0)` and Postgres `numeric(18,2)` or `double precision` for the
    same column, so both spellings are matched against the lists.
    """
    bare = " ".join(re.sub(r"\(.*?\)", " ", catalog_type.lower()).split())
    leading = _LEADING_WORD.match(bare)
    return {bare, leading.group(0)} if leading else {bare}


def classify(
    column_name: str, catalog_type: str, *, is_key: bool = False
) -> Literal["time", "dimension", "fact"]:
    """Keys group and join rather than aggregate, so a numeric key is a dimension, never a fact."""
    if column_name.lower().endswith(TIME_SUFFIXES):
        return "time"
    names = _type_names(catalog_type)
    if names & TEMPORAL_TYPES:
        return "time"
    if is_key or column_name.lower().endswith(KEY_SUFFIX):
        return "dimension"
    if names & NUMERIC_TYPES:
        return "fact"
    return "dimension"


def primary_key_columns(manifest: dict, unique_id: str) -> list[str]:
    """Columns carrying both a `unique` and a `not_null` test, in declared column order."""
    tests: dict[str, set[str]] = {}
    for node in manifest["nodes"].values():
        if node.get("resource_type") != "test" or node.get("attached_node") != unique_id:
            continue
        column = node.get("column_name")
        test_name = (node.get("test_metadata") or {}).get("name")
        if column and test_name in ("unique", "not_null"):
            tests.setdefault(column, set()).add(test_name)
    keys = {c for c, found in tests.items() if found == {"unique", "not_null"}}
    declared = list((manifest["nodes"].get(unique_id) or {}).get("columns") or {})
    return [c for c in declared if c in keys] + sorted(keys - set(declared))


def is_snapshot_derived(manifest: dict, unique_id: str) -> bool:
    nodes = manifest["nodes"]
    seen: set[str] = set()
    pending = list(((nodes.get(unique_id) or {}).get("depends_on") or {}).get("nodes") or [])
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        node = nodes.get(current) or {}
        if node.get("resource_type") == "snapshot" or current.startswith("snapshot."):
            return True
        pending.extend((node.get("depends_on") or {}).get("nodes") or [])
    return False


def _meta(element: dict) -> dict:
    """dbt 1.12 mirrors `config.meta` onto `meta`; a trimmed manifest carries only `meta`."""
    return element.get("meta") or (element.get("config") or {}).get("meta") or {}


def _synonyms(element: dict, where: str, issues: list[Issue]) -> list[str]:
    osi = _meta(element).get("osi")
    if osi is None:
        return []
    if not isinstance(osi, dict):
        issues.append(
            Issue("invalid_synonyms", f"{where}: meta.osi must map synonyms to a list; got {osi!r}")
        )
        return []
    synonyms = osi.get("synonyms")
    if synonyms is None:
        return []
    if not isinstance(synonyms, list) or not all(isinstance(s, str) and s for s in synonyms):
        issues.append(
            Issue(
                "invalid_synonyms",
                f"{where}: meta.osi.synonyms must be a list of non-empty strings; got {synonyms!r}",
            )
        )
        return []
    return synonyms


def _declared_key(alias: str, node: dict, key: object, issues: list[Issue]) -> list[str]:
    """`meta.osi.primary_key`: the declared columns that together identify a row."""
    if not isinstance(key, list) or not key or not all(isinstance(c, str) and c for c in key):
        issues.append(
            Issue(
                "invalid_primary_key",
                f"model {alias}: meta.osi.primary_key must be a non-empty list of column names; "
                f"got {key!r}",
            )
        )
        return []
    declared = {c.lower() for c in node.get("columns") or {}}
    unknown = [c for c in key if c.lower() not in declared]
    if unknown:
        issues.append(
            Issue(
                "invalid_primary_key",
                f"model {alias}: meta.osi.primary_key names {', '.join(unknown)}, "
                "which schema.yml does not declare",
            )
        )
        return []
    return key


def _primary_key(node: dict, manifest: dict, issues: list[Issue]) -> list[str]:
    """dbt turns every dataset into a semantic model, which needs an entity: the primary key."""
    alias = node["alias"]
    osi = _meta(node).get("osi")
    if isinstance(osi, dict) and "primary_key" in osi:
        return _declared_key(alias, node, osi["primary_key"], issues)
    tested = primary_key_columns(manifest, node["unique_id"])
    if not tested:
        issues.append(
            Issue(
                "missing_primary_key",
                f"mart {alias} declares no primary key; give one column both the unique and the "
                "not_null data test, or list a composite key under meta.osi.primary_key",
            )
        )
    return tested


def _catalog_entry(catalog: dict, node: dict) -> dict | None:
    nodes = catalog.get("nodes") or {}
    if node["unique_id"] in nodes:
        return nodes[node["unique_id"]]
    wanted = (node["schema"].lower(), node["alias"].lower())
    for entry in nodes.values():
        meta = entry.get("metadata") or {}
        if (str(meta.get("schema", "")).lower(), str(meta.get("name", "")).lower()) == wanted:
            return entry
    return None


def _field(
    name: str, column: dict, catalog_type: str, is_key: bool, where: str, issues: list[Issue]
) -> dict:
    field: dict = {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]},
    }
    kind = classify(name, catalog_type, is_key=is_key)
    if kind != "fact":
        field["dimension"] = {"is_time": kind == "time"}
    if column.get("description"):
        field["description"] = column["description"]
    synonyms = _synonyms(column, f"model {where} column {name}", issues)
    if synonyms:
        field["ai_context"] = {"synonyms": synonyms}
    return field


def dataset_database(config) -> str:
    """The database every generated `source` names, shared so `import` and the build agree.

    It is the database the deployed MCP reads: on Snowflake the one the platform's root
    creates, named after the project, and on Postgres the instance's `warehouse` database.
    """
    if config.warehouse.database:
        return config.warehouse.database
    if config.warehouse.kind == "snowflake":
        return config.project.name.replace("-", "_")
    return "warehouse"


def _dataset(
    node: dict, catalog: dict, database: str, schema: str, manifest: dict, issues: list[Issue]
) -> dict | None:
    alias = node["alias"]
    entry = _catalog_entry(catalog, node)
    if entry is None:
        issues.append(
            Issue(
                "not_in_catalog",
                f"model {alias} is not in the catalog; run dbt docs generate after a green build",
            )
        )
        return None
    if not isinstance(entry.get("columns"), dict):
        issues.append(
            Issue(
                "not_in_catalog",
                f"model {alias} has no columns in the catalog; "
                "run dbt docs generate after a green build",
            )
        )
        return None
    built = {name.lower(): col.get("type", "") for name, col in entry["columns"].items()}
    primary_key = _primary_key(node, manifest, issues)
    fields = []
    for name, column in (node.get("columns") or {}).items():
        if name.lower() not in built:
            issues.append(
                Issue(
                    "column_not_built",
                    f"model {alias} declares column {name} in schema.yml "
                    "but the built relation has no such column",
                )
            )
            continue
        is_key = name.lower() in {k.lower() for k in primary_key}
        fields.append(_field(name, column, built[name.lower()], is_key, alias, issues))
    dataset: dict = {"name": alias, "source": f"{database}.{schema}.{alias}"}
    if primary_key:
        dataset["primary_key"] = primary_key
    if node.get("description"):
        dataset["description"] = node["description"]
    synonyms = _synonyms(node, f"model {alias}", issues)
    if synonyms:
        dataset["ai_context"] = {"synonyms": synonyms}
    dataset["fields"] = fields
    if is_snapshot_derived(manifest, node["unique_id"]):
        dataset["custom_extensions"] = [vecten_extension({"snapshot_derived": True})]
    return dataset


def generate_datasets(
    manifest: dict,
    catalog: dict,
    *,
    database: str,
    model_name: str,
    schema: str = MARTS_SCHEMA,
) -> dict:
    """`database` is the deployed warehouse's name; the build target's database is a local one.

    `schema` both selects the mart models and names their source schema.
    """
    schema = schema.lower()
    marts = sorted(
        (
            n
            for n in manifest["nodes"].values()
            if n.get("resource_type") == "model" and str(n.get("schema", "")).lower() == schema
        ),
        key=lambda n: n["alias"],
    )
    issues: list[Issue] = []
    datasets = [_dataset(n, catalog, database, schema, manifest, issues) for n in marts]
    if issues:
        raise GeneratorError(issues)
    return {"version": OSI_VERSION, "semantic_model": [{"name": model_name, "datasets": datasets}]}


def render_document(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
