"""Fixtures: the items a resource yielded, one JSON line each, replayed without a network."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import dlt
from dlt.common.exceptions import DltException
from dlt.common.schema import Schema
from dlt.extract.hints import HintsMeta
from dlt.extract.items import TableNameMeta

from vecten_platform.sync.fixture_schema import read_fixture_schema, recorded_schema
from vecten_platform.sync.fixtures import fixture_files
from vecten_shared.records import write_text_atomic

__all__ = ["RECORD_LIMIT", "Recording", "fixture_files", "record_to", "replay_source"]

# Fixtures are real source records committed to the project, so they stay a sample.
RECORD_LIMIT = 200


def _fixture_value(value: Any) -> str:
    # dlt reads a `0x` string back into a binary column as hex; base64, its other form, can
    # itself start with `0x`.
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()
    return str(value)


class Recording:
    """An `add_map` tee that only replaces the fixture once the whole object has loaded.

    Items go to a sibling temporary file; a failed or partial run is discarded so an earlier
    good recording is never replaced by a truncated one. Past the limit items still pass
    through, so extraction and loading continue unrecorded.
    """

    def __init__(self, path: Path, limit: int = RECORD_LIMIT, table_name=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.limit = limit
        self.table_name = table_name
        self.row_tables: list[str] = []
        self.written = 0
        handle, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".recording"
        )
        self._staging = Path(name)
        self._file = open(handle, "w", encoding="utf-8")  # noqa: SIM115

    def __call__(self, item: Any, meta=None) -> Any:
        if self.written < self.limit:
            self._file.write(json.dumps(item, default=_fixture_value) + "\n")
            self.written += 1
            table_name = self.table_name
            if isinstance(meta, HintsMeta):
                table_name = meta.hints.get("table_name", table_name)
                if not meta.create_table_variant:
                    self.table_name = table_name
            if callable(table_name):
                self.row_tables.append(table_name(item))
            elif isinstance(meta, TableNameMeta):
                self.row_tables.append(meta.table_name)
            else:
                self.row_tables.append(table_name or self.path.stem)
        return item

    def commit(
        self,
        schema: Schema | None = None,
        source_nesting: int | None = None,
        *,
        input_document: str | None = None,
    ) -> bool:
        """Replace the fixture; an object that yielded nothing leaves no fixture behind."""
        self._file.close()
        try:
            # A previous row fixture survives an empty load, together with its companions.
            if not self.written and self.path.is_file():
                return False
            updates = {}
            if input_document is not None:
                input_dir = self.path.parent / "inputs"
                input_dir.mkdir(exist_ok=True)
                updates[input_dir / f"{self.path.stem}.json"] = input_document
            if self.written:
                if schema is not None:
                    try:
                        hints = recorded_schema(schema, self.row_tables, source_nesting)
                    except (DltException, ValueError, KeyError, TypeError) as exc:
                        # The runner reports the innermost exception of a chain, so a chained
                        # cause would hide the fixture path this message names.
                        raise ValueError(f"recording schema for {self.path}: {exc}") from None
                    updates[self.path.with_suffix(".schema.json")] = (
                        json.dumps(hints, indent=2, default=str) + "\n"
                    )
                updates[self.path] = self._staging.read_text(encoding="utf-8")
            with ExitStack() as rollback:
                for path, contents in updates.items():
                    previous = path.read_text(encoding="utf-8") if path.exists() else None
                    write_text_atomic(path, contents)
                    if previous is None:
                        rollback.callback(path.unlink, missing_ok=True)
                    else:
                        rollback.callback(write_text_atomic, path, previous)
                rollback.pop_all()
            return bool(self.written)
        finally:
            self._staging.unlink(missing_ok=True)

    def discard(self) -> None:
        self._file.close()
        self._staging.unlink(missing_ok=True)


def record_to(path: Path, limit: int = RECORD_LIMIT, *, table_name=None) -> Recording:
    return Recording(path, limit, table_name)


def _read(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _rows(path: Path, routes: list[str] | None) -> Callable[[], Iterator]:
    # A generator function, not a generator, so every extraction rereads the file.
    def rows() -> Iterator:
        if routes is None:
            yield from _read(path)
        else:
            for row, table in zip(_read(path), routes, strict=True):
                yield dlt.mark.with_table_name(row, table)

    return rows


def _replay_resource(path: Path, schema: Schema):
    hints_path = path.with_suffix(".schema.json")
    try:
        hints = read_fixture_schema(path)
        if hints is None:
            return dlt.resource(_rows(path, None), name=path.stem, write_disposition="append")
        tables = hints["tables"]
        for table in tables:
            schema.update_table(table)
        roots = [t for t in tables if "parent" not in t]
        first, *others = roots
        routes = hints.get("row_tables")
        if others and routes is None:
            raise ValueError("row_tables must give the destination of each fixture row")
        if routes is not None and (
            not isinstance(routes, list) or set(routes) != {t["name"] for t in roots}
        ):
            raise ValueError("row_tables must name the recorded root tables")
        nesting = hints.get("max_table_nesting")
        resource = dlt.resource(
            _rows(path, routes),
            name=path.stem,
            table_name=first["name"],
            write_disposition=first["write_disposition"],
            max_table_nesting=nesting,
        )
        for table in others:
            resource.apply_hints(
                table_name=table["name"],
                write_disposition=table["write_disposition"],
                additional_table_hints={"x-normalizer": {"max_nesting": nesting}}
                if nesting is not None
                else None,
                create_table_variant=True,
            )
        return resource
    except (DltException, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid fixture schema {hints_path}: {exc}") from exc


def replay_source(fixtures_dir: Path, connector: str):
    schema = Schema(connector)
    resources = [_replay_resource(path, schema) for path in fixture_files(fixtures_dir)]
    return dlt.source(lambda: resources, schema=schema)()
