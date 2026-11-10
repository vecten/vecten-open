import json
import shutil
from pathlib import Path

import pytest

from vecten_shared.osi.extensions import vecten_extension
from vecten_shared.osi.validate import (
    DIALECT_FOR,
    column_names,
    column_references,
    expression_for,
    is_fresh,
    validate_osi,
)

FIX = Path(__file__).parent / "fixtures" / "osi"
SEMI_ADDITIVE = '{"additivity": "semi_additive", "non_additive_over": "time", "rollup": "last"}'


@pytest.fixture
def osi(tmp_path):
    shutil.copytree(FIX, tmp_path / "osi")
    return tmp_path / "osi"


def _edit(path, fn):
    doc = json.loads(path.read_text())
    fn(doc)
    path.write_text(json.dumps(doc))


def _metric(name, expression, dialect="ANSI_SQL", **extra):
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": dialect, "expression": expression}]},
        **extra,
    }


def _add_metric(osi, metric):
    _edit(osi / "metrics.json", lambda d: d["semantic_model"][0]["metrics"].append(metric))


def test_valid_fixture_passes_with_source_check_skipped(osi):
    report = validate_osi(osi, warehouse_kind="postgres")
    assert report.ok and report.errors == []
    assert any("source existence" in n for n in report.notes)
    assert all(w.code == "semi_additive_candidate" for w in report.warnings)


def test_empty_project_passes_with_a_note(tmp_path):
    (tmp_path / "osi").mkdir()
    (tmp_path / "osi" / "datasets.json").write_text(
        json.dumps({"version": "0.1.1", "semantic_model": [{"name": "p", "datasets": []}]})
    )
    report = validate_osi(tmp_path / "osi", warehouse_kind="postgres")
    assert report.ok and any("no datasets yet" in n for n in report.notes)


def test_validate_refuses_yaml_document(osi):
    (osi / "extra.yml").write_text("metrics: []")
    report = validate_osi(osi, warehouse_kind="postgres")
    assert [e.code for e in report.errors] == ["not_json_document"]
    assert "extra.yml" in report.errors[0].document


def test_documents_are_named_relative_to_the_osi_directory(osi):
    (osi / "sub").mkdir()
    (osi / "sub" / "extra.yml").write_text("metrics: []")
    report = validate_osi(osi, warehouse_kind="postgres")
    assert report.errors[0].document == "sub/extra.yml"


def test_schema_violation_names_document_and_path(osi):
    _edit(osi / "datasets.json", lambda d: d["semantic_model"][0]["datasets"][0].pop("source"))
    report = validate_osi(osi, warehouse_kind="postgres")
    err = report.errors[0]
    assert err.code == "schema" and err.document.endswith("datasets.json")
    assert "source" in err.message
    assert "semantic_model/0/datasets/0" in err.path


def test_vendor_outside_the_enum_is_a_schema_error(osi):
    _edit(
        osi / "metrics.json",
        lambda d: d["semantic_model"][0]["metrics"][0].update(
            custom_extensions=[{"vendor_name": "vecten", "data": "{}"}]
        ),
    )
    assert validate_osi(osi, warehouse_kind="postgres").errors[0].code == "schema"


def test_duplicate_across_documents(osi):
    doc = json.loads((osi / "datasets.json").read_text())
    (osi / "sub").mkdir()
    (osi / "sub" / "more.json").write_text(json.dumps(doc))
    report = validate_osi(osi, warehouse_kind="postgres")
    err = report.errors[0]
    assert err.code == "duplicate_name"
    assert "datasets.json" in err.message and "sub/more.json" in err.message
    assert str(osi) not in err.render()


def test_home_dataset_by_coverage_and_override(osi):
    report = validate_osi(osi, warehouse_kind="postgres")
    assert report.ok
    assert report.homes == {
        "total_pipeline": "sales_pipeline",
        "deal_count": "sales_pipeline",
        "company_total": "company_overview",
    }


def test_home_dataset_by_coverage_of_bare_columns(osi):
    _add_metric(osi, _metric("won_amount", "SUM(CASE WHEN is_won THEN amount END)"))
    report = validate_osi(osi, warehouse_kind="postgres")
    assert report.ok and report.homes["won_amount"] == "sales_pipeline"


def test_validate_ambiguous_home_dataset(osi):
    _add_metric(osi, _metric("countries", "COUNT(DISTINCT country)"))
    report = validate_osi(osi, warehouse_kind="postgres")
    err = next(e for e in report.errors if e.code == "ambiguous_dataset")
    assert "countries" in err.message
    assert "sales_pipeline" in err.message and "company_overview" in err.message
    assert err.document == "metrics.json"


def test_two_qualifiers_are_ambiguous(osi):
    _add_metric(
        osi, _metric("mixed", "SUM(sales_pipeline.amount) + SUM(company_overview.total_amount)")
    )
    report = validate_osi(osi, warehouse_kind="postgres")
    assert any(e.code == "ambiguous_dataset" and "mixed" in e.message for e in report.errors)


def test_qualifier_naming_an_unknown_dataset(osi):
    _add_metric(osi, _metric("lost", "SUM(nowhere.amount)"))
    report = validate_osi(osi, warehouse_kind="postgres")
    assert any(e.code == "unknown_dataset" and "nowhere" in e.message for e in report.errors)


def test_qualified_column_the_dataset_lacks(osi):
    _add_metric(osi, _metric("wrong", "SUM(sales_pipeline.total_amount)"))
    report = validate_osi(osi, warehouse_kind="postgres")
    assert any(e.code == "unknown_field" and "total_amount" in e.message for e in report.errors)


def test_unknown_column_is_an_error(osi):
    _add_metric(osi, _metric("ghost", "SUM(nothing)"))
    report = validate_osi(osi, warehouse_kind="postgres")
    assert any(e.code == "unknown_field" and "ghost" in e.message for e in report.errors)


def test_unparsable_expression_is_an_error(osi):
    _add_metric(osi, _metric("broken", "SUM("))
    report = validate_osi(osi, warehouse_kind="postgres")
    assert any(e.code == "expression_invalid" and "broken" in e.message for e in report.errors)


def test_override_names_missing_dataset(osi):
    _edit(
        osi / "metrics.json",
        lambda d: d["semantic_model"][0]["metrics"][2]["custom_extensions"][0].update(
            data='{"vecten": {"dataset": "nope"}}'
        ),
    )
    assert any(
        e.code == "unknown_dataset" for e in validate_osi(osi, warehouse_kind="postgres").errors
    )


def test_relationship_to_unknown_dataset_and_field(osi):
    rels = [
        {
            "name": "deal_company",
            "from": "sales_pipeline",
            "to": "companies",
            "from_columns": ["company_name"],
            "to_columns": ["name"],
        },
        {
            "name": "deal_company_by_id",
            "from": "sales_pipeline",
            "to": "company_overview",
            "from_columns": ["company_id"],
            "to_columns": ["company_id"],
        },
    ]
    _edit(osi / "datasets.json", lambda d: d["semantic_model"][0].update(relationships=rels))
    codes = {e.code: e.message for e in validate_osi(osi, warehouse_kind="postgres").errors}
    assert "companies" in codes["unknown_dataset"]
    assert "company_id" in codes["unknown_field"] and "sales_pipeline" in codes["unknown_field"]


def test_dialect_coverage(osi):
    _edit(
        osi / "metrics.json",
        lambda d: d["semantic_model"][0]["metrics"][0]["expression"].update(
            dialects=[{"dialect": "SNOWFLAKE", "expression": "SUM(amount)"}]
        ),
    )
    assert validate_osi(osi, warehouse_kind="snowflake").ok
    report = validate_osi(osi, warehouse_kind="postgres")
    assert any(e.code == "dialect_missing" and "total_pipeline" in e.message for e in report.errors)


def test_expression_for_prefers_the_warehouse_dialect():
    element = {
        "expression": {
            "dialects": [
                {"dialect": "ANSI_SQL", "expression": "a"},
                {"dialect": "SNOWFLAKE", "expression": "b"},
            ]
        }
    }
    assert DIALECT_FOR["snowflake"] == ("SNOWFLAKE", "ANSI_SQL")
    assert expression_for(element, "snowflake") == "b"
    assert expression_for(element, "postgres") == "a"
    assert expression_for({"expression": {"dialects": []}}, "postgres") is None


def test_snapshot_derived_sum_warns(osi):
    report = validate_osi(osi, warehouse_kind="postgres")
    by_metric = {
        name: [w for w in report.warnings if name in w.message]
        for name in ("total_pipeline", "company_total", "deal_count")
    }
    for name in ("total_pipeline", "company_total"):
        [warning] = by_metric[name]
        assert warning.code == "semi_additive_candidate" and warning.severity == "warning"
        assert SEMI_ADDITIVE in warning.message
    assert by_metric["deal_count"] == []


def test_declared_additivity_silences_the_warnings(osi):
    _edit(
        osi / "metrics.json",
        lambda d: d["semantic_model"][0]["metrics"][0].update(
            custom_extensions=[
                {"vendor_name": "COMMON", "data": '{"vecten": {"additivity": "additive"}}'}
            ]
        ),
    )
    report = validate_osi(osi, warehouse_kind="postgres")
    assert not any("total_pipeline" in w.message for w in report.warnings)


def test_risky_expression_warns(osi):
    _add_metric(osi, _metric("avg_amount", "AVG(amount)"))
    report = validate_osi(osi, warehouse_kind="postgres")
    assert report.ok
    assert any(
        w.code == "additivity_undeclared" and "avg_amount" in w.message for w in report.warnings
    )


def test_report_as_data_is_json(osi):
    (osi / "extra.yml").write_text("metrics: []")
    data = validate_osi(osi, warehouse_kind="postgres").as_data()
    assert set(data) == {"ok", "errors", "warnings", "notes", "homes"}
    assert data["ok"] is False and data["errors"][0]["code"] == "not_json_document"
    json.dumps(data)


def test_is_fresh(osi):
    text = (osi / "datasets.json").read_text()
    assert is_fresh(osi, text)
    assert not is_fresh(osi, text.replace('"is_time": true', '"is_time": false', 1))


def test_column_names():
    assert column_names("SUM(amount) / COUNT(DISTINCT deal_id)", "ANSI_SQL") == {
        "amount",
        "deal_id",
    }
    with pytest.raises(ValueError):
        column_names("SUM(", "ANSI_SQL")


def test_column_references_keep_the_qualifier():
    assert column_references("SUM(Sales_Pipeline.Amount) + COUNT(x)", "SNOWFLAKE") == {
        ("sales_pipeline", "amount"),
        (None, "x"),
    }


def _errors(osi, metric):
    _add_metric(osi, metric)
    report = validate_osi(osi, warehouse_kind="postgres")
    return report, [e for e in report.errors if metric["name"] in e.message]


def _override(dataset):
    data = json.dumps({"vecten": {"dataset": dataset}})
    return {"custom_extensions": [{"vendor_name": "COMMON", "data": data}]}


def test_mixed_expression_with_an_unknown_qualifier(osi):
    report, errors = _errors(osi, _metric("mixed", "SUM(nowhere.amount) + COUNT(deal_id)"))
    assert [e.code for e in errors] == ["unknown_dataset"] and "nowhere" in errors[0].message
    assert "mixed" not in report.homes


def test_mixed_expression_qualifying_another_dataset(osi):
    metric = _metric("mixed", "SUM(company_overview.amount) + COUNT(deal_id)")
    report, errors = _errors(osi, metric)
    assert [e.code for e in errors] == ["ambiguous_dataset"]
    assert "sales_pipeline" in errors[0].message and "company_overview" in errors[0].message
    assert "mixed" not in report.homes


def test_override_with_a_qualifier_naming_another_dataset(osi):
    metric = _metric("pinned", "SUM(company_overview.total_amount)", **_override("sales_pipeline"))
    report, errors = _errors(osi, metric)
    assert [e.code for e in errors] == ["ambiguous_dataset"]
    assert "sales_pipeline" in errors[0].message and "company_overview" in errors[0].message
    assert "pinned" not in report.homes


def test_override_resolves_ambiguity(osi):
    metric = _metric("countries", "COUNT(DISTINCT country)", **_override("sales_pipeline"))
    report, errors = _errors(osi, metric)
    assert errors == [] and report.ok
    assert report.homes["countries"] == "sales_pipeline"


def test_missing_osi_directory_is_an_error(tmp_path):
    report = validate_osi(tmp_path / "osi", warehouse_kind="postgres")
    assert not report.ok
    assert [e.code for e in report.errors] == ["missing_osi_dir"]
    assert str(tmp_path / "osi") in report.errors[0].message


def test_hidden_files_are_skipped(osi):
    (osi / ".DS_Store").write_bytes(b"\x00\x01")
    (osi / ".cache").mkdir()
    (osi / ".cache" / "x.yml").write_text("a: 1")
    assert validate_osi(osi, warehouse_kind="postgres").ok


def _first_dataset(doc):
    return doc["semantic_model"][0]["datasets"][0]


def test_duplicate_field_in_one_dataset(osi):
    def duplicate(doc):
        fields = _first_dataset(doc)["fields"]
        fields.append(dict(fields[0]))

    _edit(osi / "datasets.json", duplicate)
    doc = json.loads((osi / "datasets.json").read_text())
    report = validate_osi(osi, warehouse_kind="postgres")
    assert [i.code for i in report.errors] == ["duplicate_field"]
    assert _first_dataset(doc)["name"] in report.errors[0].message
    assert _first_dataset(doc)["fields"][0]["name"] in report.errors[0].message


def test_key_naming_no_field(osi):
    def keys(doc):
        _first_dataset(doc)["primary_key"] = ["nope"]
        _first_dataset(doc)["unique_keys"] = [["also_nope"]]

    _edit(osi / "datasets.json", keys)
    report = validate_osi(osi, warehouse_kind="postgres")
    assert [i.code for i in report.errors] == ["unknown_key_field"]
    assert "also_nope" in report.errors[0].message and "nope," in report.errors[0].message


@pytest.mark.parametrize(
    "extension, fragment",
    [
        ({"additivity": "bogus"}, "additivity"),
        ({"additivity": "semi_additive", "rollup": "max"}, "rollup"),
        ({"additivity": "semi_additive", "non_additive_over": "region"}, "non_additive_over"),
        ({"dataset": 3}, "dataset"),
    ],
)
def test_metric_extension_values_are_checked(osi, extension, fragment):
    def set_extension(doc):
        doc["semantic_model"][0]["metrics"][0]["custom_extensions"] = [vecten_extension(extension)]

    _edit(osi / "metrics.json", set_extension)
    report = validate_osi(osi, warehouse_kind="postgres")
    flagged = [i.message for i in report.errors if i.code == "invalid_extension"]
    assert flagged and any(fragment in m for m in flagged)


@pytest.mark.parametrize(
    "extension, fragment",
    [({"snapshot_derived": "yes"}, "snapshot_derived"), ({"grain": "hour"}, "grain")],
)
def test_dataset_extension_values_are_checked(osi, extension, fragment):
    def set_extension(doc):
        _first_dataset(doc)["custom_extensions"] = [vecten_extension(extension)]

    _edit(osi / "datasets.json", set_extension)
    report = validate_osi(osi, warehouse_kind="postgres")
    assert any(i.code == "invalid_extension" and fragment in i.message for i in report.errors)
