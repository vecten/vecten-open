"""Fixtures: the items a resource yielded, one JSON line each, replayed without a network."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import dlt

from vecten_platform.sync.fixtures import fixture_files
from vecten_shared.records import write_text_atomic

__all__ = ["RECORD_LIMIT", "Recording", "fixture_files", "record_to", "replay_source"]

# Fixtures are real source records committed to the project, so they stay a sample.
RECORD_LIMIT = 200


class Recording:
    """An `add_map` tee that only replaces the fixture once the whole object has loaded.

    Items go to a sibling temporary file; a failed or partial run is discarded so an earlier
    good recording is never replaced by a truncated one. Past the limit items still pass
    through, so extraction and loading continue unrecorded.
    """

    def __init__(self, path: Path, limit: int = RECORD_LIMIT):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.limit = limit
        self.written = 0
        handle, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".recording"
        )
        self._staging = Path(name)
        self._file = open(handle, "w", encoding="utf-8")  # noqa: SIM115

    def __call__(self, item: Any) -> Any:
        if self.written < self.limit:
            self._file.write(json.dumps(item, default=str) + "\n")
            self.written += 1
        return item

    def commit(self) -> bool:
        """Replace the fixture; an object that yielded nothing leaves no fixture behind."""
        self._file.close()
        try:
            if not self.written:
                return False
            write_text_atomic(self.path, self._staging.read_text(encoding="utf-8"))
            return True
        finally:
            self._staging.unlink(missing_ok=True)

    def discard(self) -> None:
        self._file.close()
        self._staging.unlink(missing_ok=True)


def record_to(path: Path, limit: int = RECORD_LIMIT) -> Recording:
    return Recording(path, limit)


def _read(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _rows(path: Path) -> Callable[[], Iterator[dict]]:
    # A generator function, not a generator, so every extraction rereads the file.
    def rows() -> Iterator[dict]:
        yield from _read(path)

    return rows


def replay_source(fixtures_dir: Path, connector: str):
    resources = [
        dlt.resource(_rows(path), name=path.stem, write_disposition="append")
        for path in fixture_files(fixtures_dir)
    ]
    return dlt.source(lambda: resources, name=connector)()
