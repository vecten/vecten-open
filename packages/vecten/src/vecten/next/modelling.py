"""The modelling items `next` reports, from project files and the local build record only."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from vecten.connectors.sources_yml import declared_tables
from vecten.next.items import Item
from vecten_shared.config import VectenConfig
from vecten_shared.records import read_record

BUILD_RECORD = "dbt-build-local"
BUILD_COMMAND = "vecten dbt-build-local"
DESCRIBED_LAYERS = ("core", "marts")


def sha256_of(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _no_staging_items(dbt_dir: Path, config: VectenConfig) -> list[Item]:
    staging = dbt_dir / "models" / "staging"
    items: list[Item] = []
    for decl in config.connectors:
        for table in declared_tables(dbt_dir, decl.name):
            if not (staging / f"staging_{decl.name}_{table}.sql").is_file():
                title = f"Model `{table}` from `raw_{decl.name}.{table}`"
                items.append(Item("skill", title, "model"))
    return items


def _no_snapshot_items(dbt_dir: Path) -> list[Item]:
    items: list[Item] = []
    for staging in sorted((dbt_dir / "models" / "staging").glob("staging_*.sql")):
        rest = staging.stem.removeprefix("staging_")
        if not (dbt_dir / "snapshots" / f"snapshot_{rest}.sql").is_file():
            items.append(Item("skill", f"Snapshot staging_{rest}", "model"))
    return items


def _unread_snapshot_items(dbt_dir: Path) -> list[Item]:
    core = dbt_dir / "models" / "core"
    core_sql = [p.read_text(encoding="utf-8") for p in sorted(core.rglob("*.sql"))]
    items: list[Item] = []
    for snapshot in sorted((dbt_dir / "snapshots").glob("snapshot_*.sql")):
        ref = re.compile(r"ref\(\s*(['\"])" + re.escape(snapshot.stem) + r"\1\s*\)")
        if not any(ref.search(text) for text in core_sql):
            items.append(Item("skill", f"No core model reads {snapshot.stem}", "model"))
    return items


def _described(text: object) -> bool:
    return isinstance(text, str) and bool(text.strip())


def _schema_entries(directory: Path) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for path in sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")]):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError, UnicodeDecodeError:
            # The file contributes no entries, so its models read as undescribed until it parses;
            # the next `dbt-build-local` names the parse error.
            continue
        models = doc.get("models") if isinstance(doc, dict) else None
        for entry in models if isinstance(models, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                entries.setdefault(entry["name"], entry)
    return entries


def _first_gap(entry: dict | None) -> tuple[bool, str | None]:
    """Whether the entry needs describing, and the first declared column without a description."""
    if entry is None or not _described(entry.get("description")):
        return True, None
    columns = entry.get("columns")
    for column in columns if isinstance(columns, list) else []:
        if isinstance(column, dict) and not _described(column.get("description")):
            name = column.get("name")
            return True, name if isinstance(name, str) else None
    return False, None


def _undescribed_items(dbt_dir: Path) -> list[Item]:
    items: list[Item] = []
    for layer in DESCRIBED_LAYERS:
        for model in sorted((dbt_dir / "models" / layer).rglob("*.sql")):
            missing, column = _first_gap(_schema_entries(model.parent).get(model.stem))
            if missing:
                suffix = f" (column {column})" if column else ""
                items.append(Item("skill", f"Describe {model.stem}{suffix}", "model"))
    return items


def modelling_items(root: Path, config: VectenConfig) -> list[Item]:
    if not config.has("platform"):
        return []
    dbt_dir = root / "dbt"
    items = [
        *_no_staging_items(dbt_dir, config),
        *_no_snapshot_items(dbt_dir),
        *_unread_snapshot_items(dbt_dir),
        *_undescribed_items(dbt_dir),
    ]
    record = read_record(root, BUILD_RECORD)
    if not record or not record.get("ok"):
        items.append(Item("command", "Build the dbt project locally", BUILD_COMMAND))
    return items
