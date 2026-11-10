import builtins
from pathlib import Path

import dlt

from vecten_platform.sync.recording import RECORD_LIMIT, fixture_files, record_to, replay_source


def test_record_to_tees_items_and_returns_them_unchanged(tmp_path):
    tee = record_to(tmp_path / "x.jsonl")
    assert tee({"a": 1}) == {"a": 1}
    assert tee({"b": Path("p")}) == {"b": Path("p")}
    assert not (tmp_path / "x.jsonl").exists()
    tee.commit()
    assert (tmp_path / "x.jsonl").read_text().splitlines() == ['{"a": 1}', '{"b": "p"}']
    assert [p.name for p in tmp_path.iterdir()] == ["x.jsonl"]


def test_discarded_recording_leaves_the_previous_one(tmp_path):
    (tmp_path / "x.jsonl").write_text('{"old": 1}\n')
    tee = record_to(tmp_path / "x.jsonl")
    tee({"partial": 1})
    tee.discard()
    assert (tmp_path / "x.jsonl").read_text() == '{"old": 1}\n'
    assert [p.name for p in tmp_path.iterdir()] == ["x.jsonl"]


def test_recording_keeps_at_most_the_limit_and_still_returns_every_item(tmp_path):
    assert RECORD_LIMIT == 200
    tee = record_to(tmp_path / "x.jsonl")
    assert [tee({"i": i}) for i in range(300)] == [{"i": i} for i in range(300)]
    assert tee.commit() is True
    lines = (tmp_path / "x.jsonl").read_text().splitlines()
    assert len(lines) == 200 and lines[-1] == '{"i": 199}'


def test_recording_limit_is_a_parameter(tmp_path):
    tee = record_to(tmp_path / "x.jsonl", limit=2)
    for i in range(5):
        tee({"i": i})
    tee.commit()
    assert len((tmp_path / "x.jsonl").read_text().splitlines()) == 2


def test_recording_opens_its_staging_file_once(tmp_path, monkeypatch):
    tee = record_to(tmp_path / "x.jsonl")
    opened = []
    real_open, real_path_open = builtins.open, Path.open

    def counting_open(*args, **kwargs):
        opened.append(args[0] if args else None)
        return real_open(*args, **kwargs)

    def counting_path_open(self, *args, **kwargs):
        opened.append(self)
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    monkeypatch.setattr(Path, "open", counting_path_open)
    for i in range(3):
        tee({"i": i})
    monkeypatch.undo()
    assert opened == []
    tee.commit()
    assert len((tmp_path / "x.jsonl").read_text().splitlines()) == 3


def test_a_recording_with_no_items_writes_no_fixture(tmp_path):
    tee = record_to(tmp_path / "x.jsonl")
    assert tee.commit() is False
    assert list(tmp_path.iterdir()) == []


def test_replay_source_can_be_extracted_twice(tmp_path):
    (tmp_path / "companies.jsonl").write_text('{"id": 1}\n{"id": 2}\n')
    src = replay_source(tmp_path, "hubspot")
    assert list(src.resources["companies"]) == [{"id": 1}, {"id": 2}]
    assert list(src.resources["companies"]) == [{"id": 1}, {"id": 2}]


def test_replay_source_has_one_resource_per_fixture_file(tmp_path):
    (tmp_path / "companies.jsonl").write_text('{"id": 1}\n{"id": 2}\n')
    (tmp_path / "deals.jsonl").write_text('{"id": 7}\n')
    (tmp_path / "notes.txt").write_text("ignored")
    assert [p.name for p in fixture_files(tmp_path)] == ["companies.jsonl", "deals.jsonl"]
    src = replay_source(tmp_path, "hubspot")
    assert list(src.resources) == ["companies", "deals"]
    pipe = dlt.pipeline(
        "replay_test",
        destination=dlt.destinations.duckdb(str(tmp_path / "r.duckdb")),
        dataset_name="raw_hubspot",
        pipelines_dir=str(tmp_path / "dlt"),
    )
    pipe.run(src)
    counts = {
        k: v
        for k, v in pipe.last_trace.last_normalize_info.row_counts.items()
        if not k.startswith("_dlt")
    }
    assert counts == {"companies": 2, "deals": 1}


def test_replay_source_with_no_files_has_no_resources(tmp_path):
    assert list(replay_source(tmp_path, "x").resources) == []
