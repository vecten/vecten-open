"""The dbt sources file: one entry per connector, the tables a sync has landed."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic

SOURCES_YML = Path("models") / "staging" / "sources.yml"
# dlt's load id is the load's start time in epoch seconds, written as a string.
LOADED_AT_FIELD = "to_timestamp(cast(_dlt_load_id as decimal(20, 6)))"
FRESHNESS = {
    "warn_after": {"count": 36, "period": "hour"},
    "error_after": {"count": 72, "period": "hour"},
}


def _load(path: Path) -> dict:
    if not path.is_file():
        return {"version": 2, "sources": []}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise Failure(ExitCode.CONFIG, f"{path}: {exc}") from None
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise Failure(
            ExitCode.CONFIG, f"{path}: the document must be a mapping with a sources list"
        )
    doc.setdefault("version", 2)
    if doc.get("sources") is None:
        doc["sources"] = []
    if not isinstance(doc["sources"], list):
        raise Failure(ExitCode.CONFIG, f"{path}: sources must be a list")
    return doc


def load_sources(dbt_dir: Path) -> dict:
    """Parses and checks the sources file, so a caller can refuse before changing anything."""
    return _load(dbt_dir / SOURCES_YML)


def _save(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, yaml.safe_dump(doc, sort_keys=False))


def _entry(doc: dict, name: str) -> dict | None:
    return next((s for s in doc["sources"] if isinstance(s, dict) and s.get("name") == name), None)


def ensure_source(dbt_dir: Path, name: str) -> Path:
    path = dbt_dir / SOURCES_YML
    doc = _load(path)
    if _entry(doc, name) is None:
        doc["sources"].append(
            {
                "name": name,
                "schema": f"raw_{name}",
                "config": {
                    "loaded_at_field": LOADED_AT_FIELD,
                    "freshness": copy.deepcopy(FRESHNESS),
                },
                "tables": [],
            }
        )
        _save(path, doc)
    return path


def _tables(entry: dict | None) -> list[dict]:
    tables = (entry or {}).get("tables") or []
    return (
        [t for t in tables if isinstance(t, dict) and "name" in t]
        if isinstance(tables, list)
        else []
    )


def declared_tables(dbt_dir: Path, name: str) -> list[str]:
    return sorted(t["name"] for t in _tables(_entry(_load(dbt_dir / SOURCES_YML), name)))


def add_tables(dbt_dir: Path, name: str, tables: list[str]) -> list[str]:
    path = ensure_source(dbt_dir, name)
    doc = _load(path)
    entry = _entry(doc, name)
    existing = {t["name"]: t for t in _tables(entry)}
    for table in tables:
        existing.setdefault(table, {"name": table})
    entry["tables"] = [existing[k] for k in sorted(existing)]
    _save(path, doc)
    return sorted(existing)
