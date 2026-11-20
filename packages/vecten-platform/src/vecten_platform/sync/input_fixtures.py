"""Capture JSON rows at the connector's read boundary and verify its output offline."""

from __future__ import annotations

import hashlib
import json
import socket
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from functools import partial
from inspect import signature
from itertools import islice
from pathlib import Path
from unittest.mock import patch

import dlt
from dlt.common import json as dlt_json
from dlt.common.configuration.container import Container
from dlt.common.pipeline import PipelineContext

from vecten_platform.sync import context
from vecten_platform.sync.loader import load_connector
from vecten_platform.sync.recording import RECORD_LIMIT
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic


def live_inputs(fetch: Callable[[], Iterable]) -> Iterable:
    """Default for a connector's source(*, inputs=live_inputs) factory."""
    return fetch()


class InputSample:
    def __init__(self, saved_rows: list | None = None):
        self.saved_rows = saved_rows
        self.rows: list = []
        self.used = False
        self.output = hashlib.sha256()
        self.count = 0

    def inputs(self, fetch: Callable[[], Iterable]) -> Iterator:
        if self.used:
            raise ValueError("use inputs(fetch) once per object; paginate inside fetch")
        self.used = True
        if self.saved_rows is not None:
            yield from self.saved_rows
            return
        for row in islice(fetch(), RECORD_LIMIT):
            # Snapshot before the connector can mutate the row in place.
            try:
                copy = json.loads(json.dumps(row, allow_nan=False))
            except TypeError, ValueError:
                raise ValueError("input fixtures require JSON values") from None
            if copy != row:
                raise ValueError("input fixtures require JSON values with string object keys")
            self.rows.append(copy)
            yield row

    def observe(self, row):
        self.output.update(dlt_json.typed_dumps(row, sort_keys=True).encode("utf-8") + b"\n")
        self.count += 1
        return row

    def source(self, factory, name: str):
        if Path(name).name != name or "\\" in name:
            raise ValueError("object name must be a single filename to record inputs")

        try:
            signature(factory).bind(inputs=self.inputs)
        except TypeError:
            raise ValueError("input recording needs source(*, inputs=live_inputs)") from None
        source = factory(inputs=self.inputs)
        if name not in source.resources:
            raise ValueError(f"connector no longer declares object {name}")
        source.resources[name].add_map(self.observe)
        return source

    def result(self) -> dict:
        if not self.used:
            raise ValueError("the selected object did not read its rows through inputs(fetch)")
        return {"rows": self.count, "sha256": self.output.hexdigest()}

    def save(self, fixtures: Path, name: str) -> None:
        document = {"version": 1, "inputs": self.rows, "expected": self.result()}
        directory = fixtures / "inputs"
        directory.mkdir(parents=True, exist_ok=True)
        write_text_atomic(directory / f"{name}.json", json.dumps(document, indent=2) + "\n")


def _offline(*args, **kwargs):
    raise RuntimeError("network access is disabled while replaying connector inputs")


class _OfflineProvider:
    def get_secret(self, name):
        raise ValueError(
            f"read {name} inside the live fetch callback to replay without credentials"
        )


def _replay(path: Path, connector_dir: Path, connector: str, temp: Path) -> None:
    from vecten_platform.sync.runner import run_object

    doc = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(doc, dict)
        or doc.get("version") != 1
        or not isinstance(doc.get("inputs"), list)
    ):
        raise ValueError("expected a version 1 input fixture with an inputs list")
    sample = InputSample(doc["inputs"])
    connectors = connector_dir.parent
    runtime = context.RuntimeContext(
        connectors.parent, connectors, "local", _OfflineProvider(), temp
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(context, "_RUNTIME", runtime))
        stack.enter_context(Container().injectable_context(PipelineContext()))
        for owner, method in (
            (socket, "getaddrinfo"),
            (socket, "create_connection"),
            (socket.socket, "sendto"),
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
        ):
            stack.enter_context(patch.object(owner, method, _offline))
        factory = load_connector(connectors, connector).source
        result = run_object(
            connector,
            partial(sample.source, factory, path.stem),
            path.stem,
            dlt.destinations.duckdb(str(temp / "replay.duckdb")),
            temp / "dlt",
        )
    if not result.ok:
        raise ValueError(result.error)
    if sample.result() != doc["expected"]:
        raise ValueError("transformed output differs from the recorded rows")


def verify_input_fixtures(connector_dir: Path, connector: str, work: Path) -> bool:
    """Return whether any input samples were verified."""
    paths = sorted((connector_dir / "fixtures" / "inputs").glob("*.json"))
    for path in paths:
        try:
            work.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=work) as directory:
                _replay(path, connector_dir, connector, Path(directory))
        except (OSError, ValueError, KeyError, TypeError, Failure) as exc:
            raise Failure(ExitCode.TOOL_FAILED, f"replaying connector input {path}: {exc}") from exc
    return bool(paths)
