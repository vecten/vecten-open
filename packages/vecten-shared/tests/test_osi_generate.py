import json
import re
from pathlib import Path

import pytest

from vecten_shared.osi.extensions import read_vecten_extension
from vecten_shared.osi.generate import (
    GeneratorError,
    classify,
    generate_datasets,
    is_snapshot_derived,
    primary_key_columns,
    render_document,
)

FIX = Path(__file__).parent / "fixtures"

MANIFEST_NODE_KEYS = (
    "resource_type",
    "name",
    "alias",
    "schema",
    "database",
    "description",
    "meta",
    "unique_id",
    "path",
    "package_name",
    "attached_node",
    "column_name",
    "test_metadata",
)


def trim_manifest(manifest: dict) -> dict:
    """Keep what the generator reads; the result carries no paths or timestamps of the build."""
    nodes = {}
    for unique_id, node in manifest["nodes"].items():
        kept = {k: node[k] for k in MANIFEST_NODE_KEYS if k in node}
        if "columns" in node:
            kept["columns"] = {
                name: {k: col[k] for k in ("name", "description", "meta") if k in col}
                for name, col in node["columns"].items()
            }
        if "depends_on" in node:
            kept["depends_on"] = {"nodes": list(node["depends_on"].get("nodes") or [])}
        nodes[unique_id] = kept
    return {"nodes": nodes}


def trim_catalog(catalog: dict) -> dict:
    return {
        "nodes": {
            unique_id: {
                "metadata": {k: node["metadata"][k] for k in ("schema", "name")},
                "columns": {
                    name: {k: col[k] for k in ("name", "type", "index")}
                    for name, col in node["columns"].items()
                },
            }
            for unique_id, node in catalog["nodes"].items()
        }
    }


def _artifacts():
    return (
        json.loads((FIX / "dbt" / "manifest.json").read_text()),
        json.loads((FIX / "dbt" / "catalog.json").read_text()),
    )


def test_committed_artifacts_are_trimmed_and_carry_no_machine_paths():
    manifest, catalog = _artifacts()
    assert trim_manifest(manifest) == manifest and trim_catalog(catalog) == catalog
    text = (FIX / "dbt" / "manifest.json").read_text() + (FIX / "dbt" / "catalog.json").read_text()
    assert "/Users/" not in text and "/tmp" not in text and "generated_at" not in text
    kinds = {n["resource_type"] for n in manifest["nodes"].values()}
    assert {"model", "snapshot", "test"} <= kinds


def test_classify():
    assert classify("created_at", "TEXT") == "time"
    assert classify("close_date", "character varying") == "time"
    assert classify("born", "DATE") == "time"
    assert classify("amount", "NUMBER") == "fact" and classify("amount", "numeric") == "fact"
    assert classify("deal_count", "bigint") == "fact"
    assert classify("share", "double precision") == "fact"
    assert classify("country", "TEXT") == "dimension"
    assert classify("is_won", "boolean") == "dimension"
    assert classify("deal_id", "NUMBER") == "dimension"
    assert classify("code", "integer", is_key=True) == "dimension"
    assert classify("code", "integer") == "fact"
    assert classify("opened_at", "NUMBER", is_key=True) == "time"


def test_classify_reads_the_leading_word_of_parameterised_types():
    assert classify("amount", "numeric(18,2)") == "fact"
    assert classify("amount", "NUMBER(38,0)") == "fact"
    assert classify("seen", "timestamp without time zone") == "time"
    assert classify("seen", "TIMESTAMP_NTZ") == "time"
    assert classify("label", "character varying(255)") == "dimension"


def test_golden_datasets_document():
    manifest, catalog = _artifacts()
    doc = generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    assert render_document(doc) == (FIX / "osi" / "datasets.json").read_text()


def test_generated_shape():
    manifest, catalog = _artifacts()
    doc = generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    assert doc["version"] == "0.1.1" and [sm["name"] for sm in doc["semantic_model"]] == ["acme"]
    datasets = {d["name"]: d for d in doc["semantic_model"][0]["datasets"]}
    assert sorted(datasets) == ["company_overview", "sales_pipeline"]
    sp = datasets["sales_pipeline"]
    assert sp["source"] == "warehouse.marts.sales_pipeline" and sp["primary_key"] == ["deal_id"]
    fields = {f["name"]: f for f in sp["fields"]}
    assert fields["close_date"]["dimension"] == {"is_time": True}
    assert fields["created_at"]["dimension"] == {"is_time": True}
    assert fields["country"]["dimension"] == {"is_time": False}
    assert "dimension" not in fields["amount"]
    assert fields["deal_id"]["dimension"] == {"is_time": False}
    company_fields = {f["name"]: f for f in datasets["company_overview"]["fields"]}
    assert company_fields["company_id"]["dimension"] == {"is_time": False}
    assert fields["amount"]["ai_context"] == {"synonyms": ["value", "deal size"]}
    assert fields["country"]["expression"] == {
        "dialects": [{"dialect": "ANSI_SQL", "expression": "country"}]
    }
    assert "datatype" not in fields["amount"]
    assert fields["amount"]["description"]
    assert "metrics" not in doc["semantic_model"][0]
    assert read_vecten_extension(sp) == {"snapshot_derived": True}
    assert "primary_key" not in datasets["company_overview"] or datasets["company_overview"][
        "primary_key"
    ] == ["company_id"]


def test_database_comes_from_the_argument_not_the_build():
    manifest, catalog = _artifacts()
    assert all(
        n["database"].lower() == "warehouse"
        for n in manifest["nodes"].values()
        if n["resource_type"] == "model"
    )
    doc = generate_datasets(manifest, catalog, database="ANALYTICS_PROD", model_name="acme")
    assert doc["semantic_model"][0]["datasets"][0]["source"].startswith("ANALYTICS_PROD.marts.")


def test_generate_refuses_declared_column_missing_from_catalog():
    manifest, catalog = _artifacts()
    node = next(n for n in manifest["nodes"].values() if n.get("alias") == "sales_pipeline")
    node["columns"]["ghost"] = {"name": "ghost", "description": "Not built.", "meta": {}}
    with pytest.raises(GeneratorError) as exc:
        generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    assert "sales_pipeline" in exc.value.issues[0].message
    assert "ghost" in exc.value.issues[0].message


def test_generate_refuses_bad_synonyms():
    manifest, catalog = _artifacts()
    node = next(n for n in manifest["nodes"].values() if n.get("alias") == "sales_pipeline")
    node["columns"]["country"]["meta"] = {"osi": {"synonyms": "market"}}
    with pytest.raises(GeneratorError) as exc:
        generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    assert "synonyms" in exc.value.issues[0].message


def test_generate_refuses_a_mart_missing_from_the_catalog():
    manifest, catalog = _artifacts()
    del catalog["nodes"]["model.acme.company_overview"]
    with pytest.raises(GeneratorError) as exc:
        generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    assert [i.message for i in exc.value.issues] == [
        "model company_overview is not in the catalog; run dbt docs generate after a green build"
    ]


def test_generate_refuses_a_catalog_entry_without_columns():
    manifest, catalog = _artifacts()
    del catalog["nodes"]["model.acme.sales_pipeline"]["columns"]
    with pytest.raises(GeneratorError) as exc:
        generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    assert "sales_pipeline" in exc.value.issues[0].message


def test_source_uses_the_lower_cased_marts_schema():
    manifest, catalog = _artifacts()
    manifest["nodes"]["model.acme.sales_pipeline"]["schema"] = "MARTS"
    doc = generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    sources = [d["source"] for d in doc["semantic_model"][0]["datasets"]]
    assert "warehouse.marts.sales_pipeline" in sources


def test_schema_selects_and_names_marts():
    manifest, catalog = _artifacts()
    for node in manifest["nodes"].values():
        if str(node.get("schema", "")).lower() == "marts":
            node["schema"] = "Analytics"
    doc = generate_datasets(manifest, catalog, database="dw", model_name="acme", schema="analytics")
    sources = [d["source"] for d in doc["semantic_model"][0]["datasets"]]
    assert sources and all(s.startswith("dw.analytics.") for s in sources)
    default = generate_datasets(manifest, catalog, database="dw", model_name="acme")
    assert default["semantic_model"][0]["datasets"] == []


def test_dataset_database_matches_the_local_build_default():
    from types import SimpleNamespace

    from vecten_shared.osi.generate import dataset_database

    config = SimpleNamespace(
        project=SimpleNamespace(name="acme"),
        warehouse=SimpleNamespace(kind="postgres", database=None),
    )
    assert dataset_database(config) == "warehouse"
    config.warehouse.database = "analytics_dw"
    assert dataset_database(config) == "analytics_dw"


@pytest.mark.parametrize(
    ("project", "database"), [("acme", None), ("acme-data", None), ("acme", "analytics_dw")]
)
def test_snowflake_sources_name_the_database_the_root_creates(project, database):
    from vecten_shared.config import VectenConfig
    from vecten_shared.osi.generate import dataset_database
    from vecten_shared.provider import snowflake_names

    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": project, "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "database": database},
        }
    )
    created = snowflake_names(config, "prod").database
    assert dataset_database(config).upper() == created
    # The Snowflake root takes only an unquoted identifier as the database name.
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*", created)


def test_catalog_falls_back_to_schema_and_alias():
    manifest, catalog = _artifacts()
    expected = generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    catalog["nodes"]["renamed"] = catalog["nodes"].pop("model.acme.sales_pipeline")
    actual = generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    assert actual == expected


def test_model_synonyms_become_dataset_ai_context():
    manifest, catalog = _artifacts()
    manifest["nodes"]["model.acme.company_overview"]["meta"] = {"osi": {"synonyms": ["accounts"]}}
    doc = generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    co = next(d for d in doc["semantic_model"][0]["datasets"] if d["name"] == "company_overview")
    assert co["ai_context"] == {"synonyms": ["accounts"]}
    assert list(co)[: list(co).index("fields")][-1] == "ai_context"


def test_primary_key_and_snapshot_lineage_helpers():
    manifest, _ = _artifacts()
    assert primary_key_columns(manifest, "model.acme.sales_pipeline") == ["deal_id"]
    assert primary_key_columns(manifest, "model.acme.staging_demo_deal") == []
    assert is_snapshot_derived(manifest, "model.acme.company_overview")
    assert not is_snapshot_derived(manifest, "model.acme.staging_demo_deal")
    assert not is_snapshot_derived(manifest, "model.vecten_convention.metricflow_time_spine")


def test_generated_document_validates_against_the_pinned_schema():
    import jsonschema

    from vecten_shared.osi import load_schema

    manifest, catalog = _artifacts()
    jsonschema.validate(
        generate_datasets(manifest, catalog, database="warehouse", model_name="acme"),
        load_schema(),
    )


def _sales_pipeline(manifest: dict) -> dict:
    return manifest["nodes"]["model.acme.sales_pipeline"]


def test_a_composite_key_comes_from_model_meta():
    manifest, catalog = _artifacts()
    _sales_pipeline(manifest)["meta"] = {"osi": {"primary_key": ["country", "stage"]}}
    doc = generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    sp = next(d for d in doc["semantic_model"][0]["datasets"] if d["name"] == "sales_pipeline")
    assert sp["primary_key"] == ["country", "stage"]


def test_a_mart_without_a_key_is_refused_naming_both_ways_to_declare_one():
    manifest, catalog = _artifacts()
    for unique_id, node in list(manifest["nodes"].items()):
        if node.get("attached_node") == "model.acme.sales_pipeline":
            del manifest["nodes"][unique_id]
    with pytest.raises(GeneratorError) as exc:
        generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    [issue] = exc.value.issues
    assert issue.code == "missing_primary_key"
    assert "sales_pipeline" in issue.message
    assert "unique" in issue.message and "not_null" in issue.message
    assert "meta.osi.primary_key" in issue.message


@pytest.mark.parametrize(
    ("key", "named"),
    [("country", "non-empty list"), ([], "non-empty list"), (["country", "ghost"], "ghost")],
)
def test_a_declared_key_must_list_declared_columns(key, named):
    manifest, catalog = _artifacts()
    _sales_pipeline(manifest)["meta"] = {"osi": {"primary_key": key}}
    with pytest.raises(GeneratorError) as exc:
        generate_datasets(manifest, catalog, database="warehouse", model_name="acme")
    [issue] = exc.value.issues
    assert issue.code == "invalid_primary_key"
    assert "sales_pipeline" in issue.message and named in issue.message
