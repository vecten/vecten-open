import pytest
import yaml

from vecten.connectors.sources_yml import SOURCES_YML, add_tables, declared_tables, ensure_source
from vecten_shared.output import ExitCode, Failure


def test_ensure_source_creates_the_file_with_the_raw_schema(tmp_path):
    path = ensure_source(tmp_path, "hubspot")
    assert path == tmp_path / SOURCES_YML
    doc = yaml.safe_load(path.read_text())
    assert doc["version"] == 2 and len(doc["sources"]) == 1
    entry = doc["sources"][0]
    assert (entry["name"], entry["schema"], entry["tables"]) == ("hubspot", "raw_hubspot", [])


def test_ensure_source_declares_freshness(tmp_path):
    ensure_source(tmp_path, "hubspot")
    doc = yaml.safe_load((tmp_path / "models" / "staging" / "sources.yml").read_text())
    config = doc["sources"][0]["config"]
    assert config["loaded_at_field"] == "to_timestamp(cast(_dlt_load_id as decimal(20, 6)))"
    assert config["freshness"] == {
        "warn_after": {"count": 36, "period": "hour"},
        "error_after": {"count": 72, "period": "hour"},
    }


def test_ensure_source_leaves_an_existing_entry_alone(tmp_path):
    path = tmp_path / SOURCES_YML
    path.parent.mkdir(parents=True)
    path.write_text("version: 2\nsources:\n- name: hubspot\n  schema: crm\n  tables: []\n")
    ensure_source(tmp_path, "hubspot")
    doc = yaml.safe_load(path.read_text())
    assert doc["sources"] == [{"name": "hubspot", "schema": "crm", "tables": []}]


def test_ensure_source_keeps_other_sources_and_is_idempotent(tmp_path):
    ensure_source(tmp_path, "hubspot")
    ensure_source(tmp_path, "stripe")
    ensure_source(tmp_path, "hubspot")
    doc = yaml.safe_load((tmp_path / SOURCES_YML).read_text())
    assert [s["name"] for s in doc["sources"]] == ["hubspot", "stripe"]


def test_add_tables_merges_sorted_and_unique(tmp_path):
    ensure_source(tmp_path, "hubspot")
    assert add_tables(tmp_path, "hubspot", ["deals", "companies"]) == ["companies", "deals"]
    assert add_tables(tmp_path, "hubspot", ["companies", "contacts"]) == [
        "companies",
        "contacts",
        "deals",
    ]
    assert declared_tables(tmp_path, "hubspot") == ["companies", "contacts", "deals"]
    doc = yaml.safe_load((tmp_path / SOURCES_YML).read_text())
    assert doc["sources"][0]["tables"] == [
        {"name": "companies"},
        {"name": "contacts"},
        {"name": "deals"},
    ]


def test_add_tables_keeps_table_extras(tmp_path):
    ensure_source(tmp_path, "hubspot")
    path = tmp_path / SOURCES_YML
    doc = yaml.safe_load(path.read_text())
    doc["sources"][0]["tables"] = [{"name": "companies", "description": "kept"}]
    path.write_text(yaml.safe_dump(doc))
    add_tables(tmp_path, "hubspot", ["deals"])
    doc = yaml.safe_load(path.read_text())
    assert doc["sources"][0]["tables"][0] == {"name": "companies", "description": "kept"}


def test_null_sources_is_an_empty_list_and_odd_entries_are_skipped(tmp_path):
    path = tmp_path / SOURCES_YML
    path.parent.mkdir(parents=True)
    path.write_text("version: 2\nsources:\n")
    ensure_source(tmp_path, "hubspot")
    doc = yaml.safe_load(path.read_text())
    doc["sources"].insert(0, "stray")
    path.write_text(yaml.safe_dump(doc))
    assert add_tables(tmp_path, "hubspot", ["deals"]) == ["deals"]
    assert declared_tables(tmp_path, "hubspot") == ["deals"]


@pytest.mark.parametrize("text", ["- a\n", "sources: nope\n", "sources: [unclosed\n"])
def test_unusable_documents_are_config(tmp_path, text):
    path = tmp_path / SOURCES_YML
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(Failure) as exc:
        ensure_source(tmp_path, "hubspot")
    assert exc.value.code == ExitCode.CONFIG and str(path) in exc.value.message
