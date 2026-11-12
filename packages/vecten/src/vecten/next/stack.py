"""The local-stack items `next` reports: dependency records to re-lock, then starting the stack.

Both read committed files and run records only."""

from __future__ import annotations

from pathlib import Path

from vecten.next.items import Item
from vecten.stack import local_components
from vecten_shared.config import VectenConfig
from vecten_shared.records import read_record


def lock_items(root: Path, config: VectenConfig) -> list[Item]:
    """`vecten lock`, while a record is stale or project requirements have no lock."""
    from vecten import locking

    records = [locking.PINS_FILE, locking.RECORD_FILE, *locking.LOCK_FILES.values()]
    if any((root / name).exists() for name in records):
        problems = locking.lock_problems(root, config)
        if not problems:
            return []
        title = "Re-lock the project's dependencies: " + ", ".join(problems) + " are out of date"
        return [Item("command", title, "vecten lock")]
    if any(config.dependencies.python(c) for c in locking.LOCK_FILES):
        return [Item("command", "Lock the [dependencies] requirements", "vecten lock")]
    return []


def stack_items(root: Path, config: VectenConfig) -> list[Item]:
    if not local_components(config):
        return []
    # `vecten down` removes the `up` record, so a stopped stack shows as `down` alone.
    if read_record(root, "up") is None and read_record(root, "down") is None:
        return [Item("command", "Start the local stack", "vecten up")]
    return []
