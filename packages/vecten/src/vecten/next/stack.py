"""The local-stack item `next` reports, from the `up` and `down` run records only."""

from __future__ import annotations

from pathlib import Path

from vecten.next.items import Item
from vecten.stack import local_components
from vecten_shared.config import VectenConfig
from vecten_shared.records import read_record


def stack_items(root: Path, config: VectenConfig) -> list[Item]:
    if not local_components(config):
        return []
    # `vecten down` removes the `up` record, so a stopped stack shows as `down` alone.
    if read_record(root, "up") is None and read_record(root, "down") is None:
        return [Item("command", "Start the local stack", "vecten up")]
    return []
