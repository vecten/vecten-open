"""What the rendered connector test calls: replay recorded fixtures with the network off."""

from __future__ import annotations

import socket
from collections import Counter
from pathlib import Path

import dlt
import pytest

from vecten_platform.sync.fixture_schema import fixture_table_counts
from vecten_platform.sync.fixtures import fixture_files
from vecten_platform.sync.input_fixtures import verify_input_fixtures
from vecten_platform.sync.recording import replay_source


def _no_network(*args, **kwargs):
    raise RuntimeError("network access is disabled while replaying fixtures")


def block_network(monkeypatch) -> None:
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_network)


def replay_connector_test(
    connector_dir: Path, connector: str, tmp_path: Path, monkeypatch
) -> dict[str, int]:
    fixtures_dir = connector_dir / "fixtures"
    verified_inputs = verify_input_fixtures(connector_dir, connector, tmp_path)
    files = fixture_files(fixtures_dir)
    if not files:
        if verified_inputs:
            return {}
        pytest.skip(
            f"no fixtures under {fixtures_dir}; record them with "
            f"`vecten connector run {connector} --record`"
        )
    lines = {
        p: sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
        for p in files
    }
    # Recording never keeps an object that yielded nothing, so an empty file was made by hand.
    empty = [p.name for p, n in lines.items() if n == 0]
    assert not empty, f"fixtures hold no records: {', '.join(empty)}; delete or re-record them"
    block_network(monkeypatch)
    tmp_path.mkdir(parents=True, exist_ok=True)
    pipeline = dlt.pipeline(
        f"replay__{connector}",
        destination=dlt.destinations.duckdb(str(tmp_path / "replay.duckdb")),
        dataset_name=f"raw_{connector}",
        pipelines_dir=str(tmp_path / "dlt"),
    )
    pipeline.run(replay_source(fixtures_dir, connector))
    schema = pipeline.default_schema
    counts = pipeline.last_trace.last_normalize_info.row_counts
    # Several raw tables can belong to one fixture when its resource routes rows by table name.
    roots = {
        t["name"]: counts[t["name"]]
        for t in schema.data_tables()
        if not t.get("parent") and t["name"] in counts
    }
    expected = Counter()
    for path, count in lines.items():
        expected.update(fixture_table_counts(path, count, schema))
    assert roots == expected, f"replayed {roots}, fixtures hold {expected}"
    return roots
