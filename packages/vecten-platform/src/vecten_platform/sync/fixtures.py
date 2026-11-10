"""Where a connector's fixtures live; stdlib only so the CLI can ask without importing dlt."""

from __future__ import annotations

from pathlib import Path


def fixture_files(fixtures_dir: Path) -> list[Path]:
    if not fixtures_dir.is_dir():
        return []
    return sorted(fixtures_dir.glob("*.jsonl"))
