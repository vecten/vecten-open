"""What the rendered connector test calls: replay recorded fixtures with the network off."""

from __future__ import annotations

import socket
from pathlib import Path

import dlt
import pytest

from vecten_platform.sync.fixtures import fixture_files
from vecten_platform.sync.recording import replay_source
from vecten_platform.sync.runner import top_level_counts


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
    files = fixture_files(fixtures_dir)
    if not files:
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
    naming = pipeline.default_schema.naming
    expected = {naming.normalize_table_identifier(p.stem): n for p, n in lines.items()}
    # Child tables hold nested lists, whose row counts no fixture line count predicts.
    counts = top_level_counts(pipeline.last_trace.last_normalize_info.row_counts)
    assert counts == expected, f"replayed {counts}, fixtures hold {expected}"
    return counts
