"""The part of a loaded dlt schema that an offline fixture needs."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from dlt.common.normalizers.json.helpers import normalize_table_identifier
from dlt.common.schema import Schema
from dlt.common.schema.utils import get_nested_tables, remove_processing_hints


def recorded_schema(
    schema: Schema, row_tables: list[str], max_nesting: int | None
) -> dict[str, Any]:
    # A table-name hint can route different rows of one resource to different tables.
    names = {
        name: normalize_table_identifier(schema, schema.naming, name)
        for name in dict.fromkeys(row_tables)
    }
    routes = [names[name] for name in row_tables]
    roots = dict.fromkeys(routes)
    for root in roots:
        schema.get_table(root)
    tables = {
        table["name"]: deepcopy(table)
        for root in roots
        for table in get_nested_tables(schema.tables, root)
    }
    remove_processing_hints(tables)
    for table in tables.values():
        # The local warehouse chooses its own destination and file formats.
        table.pop("table_format", None)
        table.pop("file_format", None)
    hints: dict[str, Any] = {"tables": list(tables.values())}
    if max_nesting is not None:
        hints["max_table_nesting"] = max_nesting
    if len(roots) > 1:
        hints["row_tables"] = routes
    return hints


def read_fixture_schema(path: Path) -> dict[str, Any] | None:
    try:
        hints = json.loads(path.with_suffix(".schema.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(hints, dict):
        raise ValueError("expected a JSON object")
    return hints


def fixture_table_counts(path: Path, lines: int, schema: Schema) -> Counter[str]:
    hints = read_fixture_schema(path)
    if hints is None:
        name = path.stem
    elif routes := hints.get("row_tables"):
        return Counter(normalize_table_identifier(schema, schema.naming, name) for name in routes)
    else:
        name = hints["tables"][0]["name"]
    return Counter({normalize_table_identifier(schema, schema.naming, name): lines})
