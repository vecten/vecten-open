import json
from pathlib import Path

import dlt
import pytest

from vecten_platform.sync import context
from vecten_platform.sync.loader import load_connector
from vecten_platform.sync.runner import (
    _chain,
    dataset_name,
    ensure_datasets,
    list_objects,
    pipeline_name,
    run_object,
    run_objects,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.local import LocalProvider

FIXTURES = Path(__file__).parent / "fixture_connector"


@pytest.fixture
def demo(tmp_path):
    provider = LocalProvider(tmp_path, inside_compose=False)
    provider.put_secret("DEMO_TOKEN", "t")
    context.set_runtime(
        context.RuntimeContext(tmp_path, FIXTURES, "local", provider, tmp_path / "dlt")
    )
    yield load_connector(FIXTURES, "demo")
    context.set_runtime(None)


@pytest.fixture
def duckdb_dest(tmp_path):
    return dlt.destinations.duckdb(str(tmp_path / "wh.duckdb"))


def rows(dest_path: Path, table: str) -> list[tuple]:
    import duckdb

    con = duckdb.connect(str(dest_path), read_only=True)
    try:
        return con.execute(f"select * from raw_demo.{table} order by 1").fetchall()
    finally:
        con.close()


def test_names():
    assert pipeline_name("demo", "good_a") == "demo__good_a"
    assert dataset_name("demo") == "raw_demo"


def test_list_objects_in_declaration_order(demo):
    assert list_objects(demo.source()) == ["good_a", "bad", "good_b"]


def test_one_failing_object_does_not_block_or_reset_the_others(demo, duckdb_dest, tmp_path):
    outcomes = run_objects(
        "demo", demo.source, ["good_a", "bad", "good_b"], duckdb_dest, tmp_path / "dlt"
    )
    by_name = {o.object: o for o in outcomes}
    assert by_name["good_a"].ok and by_name["good_a"].rows == 2
    assert by_name["good_b"].ok and by_name["good_b"].rows == 1
    assert not by_name["bad"].ok and "upstream returned 500" in by_name["bad"].error
    assert "\n" not in by_name["bad"].error
    assert by_name["bad"].error.startswith("RuntimeError:")
    assert [r[1] for r in rows(tmp_path / "wh.duckdb", "good_a")] == ["a1", "a2"]
    # Each object is its own pipeline, so the failed one left no state and the others kept theirs.
    import duckdb

    con = duckdb.connect(str(tmp_path / "wh.duckdb"), read_only=True)
    names = {
        r[0]
        for r in con.execute("select pipeline_name from raw_demo._dlt_pipeline_state").fetchall()
    }
    con.close()
    assert names == {"demo__good_a", "demo__good_b"}


@pytest.mark.parametrize("credits, terminal", [(0, True), (1, False)])
def test_wrapped_metered_failure(demo, duckdb_dest, tmp_path, credits, terminal):
    outcome = run_object(
        "demo", lambda: demo.source(credits=credits), "bad", duckdb_dest, tmp_path / "dlt"
    )
    assert not outcome.ok
    assert outcome.terminal is terminal
    expected = (
        "TerminalConnectorError: metered source is out of credits"
        if terminal
        else "HTTPError: 503 Service Unavailable"
    )
    assert outcome.error == expected


def test_checkpoint_survives_a_fresh_dlt_dir(demo, duckdb_dest, tmp_path):
    run_object("demo", demo.source, "good_a", duckdb_dest, tmp_path / "dlt1")
    again = run_object("demo", demo.source, "good_a", duckdb_dest, tmp_path / "dlt2")
    # The incremental cursor restored from the destination filters every row already loaded.
    assert again.ok and again.rows == 0
    assert len(rows(tmp_path / "wh.duckdb", "good_a")) == 2


def test_rows_exclude_dlt_bookkeeping_tables(demo, duckdb_dest, tmp_path):
    outcome = run_object("demo", demo.source, "good_b", duckdb_dest, tmp_path / "dlt")
    assert outcome.rows == 1


def test_missing_secret_propagates_as_failure(tmp_path, duckdb_dest):
    provider = LocalProvider(tmp_path, inside_compose=False)
    context.set_runtime(
        context.RuntimeContext(tmp_path, FIXTURES, "local", provider, tmp_path / "dlt")
    )
    try:
        module = load_connector(FIXTURES, "demo")
        with pytest.raises(Failure) as exc:
            run_object("demo", module.source, "good_a", duckdb_dest, tmp_path / "dlt")
        assert exc.value.code == ExitCode.PREREQUISITE
    finally:
        context.set_runtime(None)


def test_record_writes_one_jsonl_per_object(demo, duckdb_dest, tmp_path):
    record_dir = tmp_path / "fixtures"
    run_objects(
        "demo",
        demo.source,
        ["good_a", "good_b"],
        duckdb_dest,
        tmp_path / "dlt",
        record_dir=record_dir,
    )
    lines = (record_dir / "good_a.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"id": 1, "name": "a1"},
        {"id": 2, "name": "a2"},
    ]
    assert (record_dir / "good_b.jsonl").read_text().splitlines() == ['{"id": 10, "amount": 1.5}']
    assert not (record_dir / "bad.jsonl").exists()


def test_record_replaces_a_previous_recording(demo, duckdb_dest, tmp_path):
    record_dir = tmp_path / "fixtures"
    record_dir.mkdir()
    (record_dir / "good_b.jsonl").write_text("stale\n")
    run_object("demo", demo.source, "good_b", duckdb_dest, tmp_path / "dlt", record_dir=record_dir)
    assert (record_dir / "good_b.jsonl").read_text().splitlines() == ['{"id": 10, "amount": 1.5}']


def test_record_after_a_run_reloads_the_object_from_the_start(demo, duckdb_dest, tmp_path):
    run_object("demo", demo.source, "good_a", duckdb_dest, tmp_path / "dlt")
    record_dir = tmp_path / "fixtures"
    outcome = run_object(
        "demo", demo.source, "good_a", duckdb_dest, tmp_path / "dlt", record_dir=record_dir
    )
    assert outcome.ok and outcome.rows == 2
    assert len((record_dir / "good_a.jsonl").read_text().splitlines()) == 2
    assert [r[1] for r in rows(tmp_path / "wh.duckdb", "good_a")] == ["a1", "a2"]


def test_record_skips_a_failing_object_and_keeps_its_earlier_recording(demo, duckdb_dest, tmp_path):
    record_dir = tmp_path / "fixtures"
    record_dir.mkdir()
    (record_dir / "bad.jsonl").write_text('{"kept": true}\n')
    (record_dir / "bad.schema.json").write_text('{"previous": true}')
    run_objects(
        "demo", demo.source, ["bad", "good_b"], duckdb_dest, tmp_path / "dlt", record_dir=record_dir
    )
    assert (record_dir / "bad.schema.json").read_text() == '{"previous": true}'
    assert (record_dir / "bad.jsonl").read_text() == '{"kept": true}\n'
    assert sorted(p.name for p in record_dir.iterdir()) == [
        "bad.jsonl",
        "bad.schema.json",
        "good_b.jsonl",
        "good_b.schema.json",
    ]


def test_record_of_a_failing_object_writes_no_fixture(demo, duckdb_dest, tmp_path):
    record_dir = tmp_path / "fixtures"
    run_objects(
        "demo", demo.source, ["bad", "good_b"], duckdb_dest, tmp_path / "dlt", record_dir=record_dir
    )
    assert sorted(p.name for p in record_dir.iterdir()) == ["good_b.jsonl", "good_b.schema.json"]


def test_a_schema_recording_error_reports_the_fixture_it_was_writing(
    demo, duckdb_dest, tmp_path, monkeypatch
):
    from vecten_platform.sync import recording

    def broken(*args, **kwargs):
        raise KeyError("good_b")

    monkeypatch.setattr(recording, "recorded_schema", broken)
    record_dir = tmp_path / "fixtures"
    outcome = run_object(
        "demo", demo.source, "good_b", duckdb_dest, tmp_path / "dlt", record_dir=record_dir
    )
    assert not outcome.ok
    assert outcome.error.startswith("ValueError: recording schema for ")
    assert str(record_dir / "good_b.jsonl") in outcome.error


@dlt.resource(name="lazy")
def _lazy_secret_resource():
    yield {"token": context.secret("MISSING")}


@dlt.source(name="lazy_demo")
def _lazy_source():
    return [_lazy_secret_resource]


def test_secret_missing_inside_a_resource_body_propagates_as_failure(demo, duckdb_dest, tmp_path):
    with pytest.raises(Failure) as exc:
        run_object("lazy_demo", _lazy_source, "lazy", duckdb_dest, tmp_path / "dlt")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "MISSING" in exc.value.message


@dlt.resource(name="many")
def _many():
    yield from ({"id": i} for i in range(300))


@dlt.resource(name="nothing")
def _nothing():
    yield from ()


@dlt.resource(name="interrupted")
def _interrupted():
    yield {"id": 1}
    raise KeyboardInterrupt


@dlt.source(name="sizes")
def _sizes_source():
    return [_many, _nothing, _interrupted]


def _count(dest_path: Path, table: str) -> int:
    import duckdb

    con = duckdb.connect(str(dest_path), read_only=True)
    try:
        return con.execute(f"select count(*) from raw_sizes.{table}").fetchone()[0]
    finally:
        con.close()


def test_record_caps_the_fixture_and_the_load(duckdb_dest, tmp_path):
    record_dir = tmp_path / "fixtures"
    outcome = run_object(
        "sizes", _sizes_source, "many", duckdb_dest, tmp_path / "dlt", record_dir=record_dir
    )
    assert outcome.ok and outcome.rows == 200
    assert len((record_dir / "many.jsonl").read_text().splitlines()) == 200
    assert _count(tmp_path / "wh.duckdb", "many") == 200


def test_record_of_an_object_with_no_items_leaves_no_fixture(duckdb_dest, tmp_path):
    record_dir = tmp_path / "fixtures"
    outcome = run_object(
        "sizes", _sizes_source, "nothing", duckdb_dest, tmp_path / "dlt", record_dir=record_dir
    )
    assert outcome.ok
    assert list(record_dir.iterdir()) == []


def test_an_interrupted_recording_leaves_no_staging_file(duckdb_dest, tmp_path):
    record_dir = tmp_path / "fixtures"
    with pytest.raises(KeyboardInterrupt):
        run_object(
            "sizes",
            _sizes_source,
            "interrupted",
            duckdb_dest,
            tmp_path / "dlt",
            record_dir=record_dir,
        )
    assert list(record_dir.iterdir()) == []


@dlt.resource(name="Contact-Lists")
def _contact_lists():
    yield from [
        {"id": 1, "members": [{"m": "a"}, {"m": "b"}, {"m": "c"}]},
        {"id": 2, "members": [{"m": "d"}]},
    ]


@dlt.source(name="crm")
def _crm_source():
    return [_contact_lists]


def test_outcome_names_the_top_level_tables_and_counts_their_rows(duckdb_dest, tmp_path):
    outcome = run_object("crm", _crm_source, "Contact-Lists", duckdb_dest, tmp_path / "dlt")
    assert outcome.ok
    assert outcome.tables == ("contact_lists",)
    assert outcome.rows == 2


def test_a_failed_object_lists_no_tables(demo, duckdb_dest, tmp_path):
    outcome = run_object("demo", demo.source, "bad", duckdb_dest, tmp_path / "dlt")
    assert not outcome.ok and outcome.tables == ()


def test_exception_chain_honours_from_none():
    try:
        try:
            raise KeyError("hidden")
        except KeyError:
            raise ValueError("bad config") from None
    except ValueError as exc:
        assert _chain(exc) == [exc]


def _schemas(dest_path: Path) -> set[str]:
    import duckdb

    con = duckdb.connect(str(dest_path), read_only=True)
    try:
        rows = con.execute("select schema_name from information_schema.schemata").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def test_ensure_datasets_creates_raw_and_staging_schemas_once(demo, duckdb_dest, tmp_path):
    assert ensure_datasets("demo", duckdb_dest, tmp_path / "dlt") == [
        "raw_demo",
        "raw_demo_staging",
    ]
    assert {"raw_demo", "raw_demo_staging"} <= _schemas(tmp_path / "wh.duckdb")
    # A second run finds both schemas and leaves them as they are.
    ensure_datasets("demo", duckdb_dest, tmp_path / "dlt")
    outcome = run_object("demo", demo.source, "good_a", duckdb_dest, tmp_path / "dlt")
    assert outcome.ok, outcome.error
    assert len(rows(tmp_path / "wh.duckdb", "good_a")) == 2


@dlt.source(name="crm", max_table_nesting=0)
def _json_source():
    @dlt.resource(
        name="records",
        columns={"_raw": {"data_type": "json"}},
        primary_key="id",
        write_disposition="merge",
    )
    def records():
        for _ in range(2):
            yield {"id": 1, "_raw": {"name": "Acme", "tags": ["new"]}, "labels": ["one"]}

    return records


def test_recorded_json_column_survives_offline_replay(duckdb_dest, tmp_path, monkeypatch):
    import duckdb

    from vecten_platform.sync.testing import replay_connector_test

    fixtures = tmp_path / "crm" / "fixtures"
    outcome = run_object("crm", _json_source, "records", duckdb_dest, tmp_path / "dlt", fixtures)
    assert outcome.ok, outcome.error
    work = tmp_path / "work"
    assert replay_connector_test(fixtures.parent, "crm", work, monkeypatch) == {"records": 2}
    for database in (tmp_path / "wh.duckdb", work / "replay.duckdb"):
        with duckdb.connect(str(database), read_only=True) as conn:
            columns = {
                row[0]: row[1] for row in conn.execute("describe raw_crm.records").fetchall()
            }
            assert columns["_raw"] == columns["labels"] == "JSON", columns
            assert conn.execute("select _raw->>'name' from raw_crm.records").fetchall() == [
                ("Acme",)
            ]
    replay = dlt.attach("replay__crm", pipelines_dir=str(work / "dlt"))
    table = replay.default_schema.tables["records"]
    assert table["columns"]["id"]["primary_key"]
    assert table["write_disposition"] == "merge"
    assert table["x-normalizer"]["max_nesting"] == 0
    assert replay.default_schema.data_table_names() == ["records"]


@dlt.source(name="crm", max_table_nesting=2)
def _routed_source():
    @dlt.resource(name="events", table_name=lambda row: row["kind"], max_table_nesting=0)
    def events():
        yield {"kind": "one", "labels": [1]}
        yield {"kind": "two", "labels": [2]}
        yield {"kind": "one", "labels": [3]}

    return events


def test_recorded_routes_and_legacy_fixtures_share_a_source(duckdb_dest, tmp_path, monkeypatch):
    from vecten_platform.sync.testing import replay_connector_test

    fixtures = tmp_path / "crm" / "fixtures"
    outcome = run_object("crm", _routed_source, "events", duckdb_dest, tmp_path / "dlt", fixtures)
    assert outcome.ok, outcome.error
    (fixtures / "legacy.jsonl").write_text('{"labels": [1, 2]}\n')
    work = tmp_path / "work"
    assert replay_connector_test(fixtures.parent, "crm", work, monkeypatch) == {
        "one": 2,
        "two": 1,
        "legacy": 1,
    }
    schema = dlt.attach("replay__crm", pipelines_dir=str(work / "dlt")).default_schema
    assert set(schema.data_table_names()) == {"one", "two", "legacy", "legacy__labels"}
    for name in ("one", "two"):
        assert schema.tables[name]["x-normalizer"]["max_nesting"] == 0


@pytest.mark.parametrize("variant", [False, True])
def test_record_and_replay_midstream_table_hint(variant, duckdb_dest, tmp_path, monkeypatch):
    import duckdb

    from vecten_platform.sync.testing import replay_connector_test

    @dlt.resource(name="items")
    def items():
        yield dlt.mark.with_hints(
            {"id": 1}, dlt.mark.make_hints(table_name="renamed"), create_table_variant=variant
        )
        yield {"id": 2}
        yield {"id": 3}

    @dlt.source(name="crm")
    def source():
        return items

    fixtures = tmp_path / "crm" / "fixtures"
    outcome = run_object("crm", source, "items", duckdb_dest, tmp_path / "dlt", fixtures)
    assert outcome.ok, outcome.error
    expected = ["renamed", "items", "items"] if variant else ["renamed"] * 3
    schema = json.loads((fixtures / "items.schema.json").read_text())
    assert schema.get("row_tables", [schema["tables"][0]["name"]] * 3) == expected
    work = tmp_path / "replay"
    counts = {name: expected.count(name) for name in set(expected)}
    assert set(outcome.tables) == set(counts)
    assert replay_connector_test(fixtures.parent, "crm", work, monkeypatch) == counts
    for database in (tmp_path / "wh.duckdb", work / "replay.duckdb"):
        with duckdb.connect(str(database), read_only=True) as conn:
            for name in counts:
                rows = conn.execute(f"select id from raw_crm.{name} order by id").fetchall()
                assert rows == [(i,) for i, table in enumerate(expected, start=1) if table == name]
