import json

import pytest

from vecten_shared.osi import OSI_VERSION, load_schema
from vecten_shared.osi.documents import list_documents, load_document, merge
from vecten_shared.osi.issues import ModelError
from vecten_shared.versions import VERSIONS


def test_pinned_schema_matches_the_table():
    assert OSI_VERSION == VERSIONS.ossie_spec
    schema = load_schema()
    assert schema["required"] == ["version", "semantic_model"]
    assert "GOODDATA" in schema["$defs"]["Vendor"]["enum"]
    assert "POSTGRES" not in schema["$defs"]["Dialect"]["enum"]


def _doc(name, datasets=(), metrics=(), relationships=(), version="0.1.1"):
    return {
        "version": version,
        "semantic_model": [
            {
                "name": name,
                "datasets": list(datasets),
                "metrics": list(metrics),
                "relationships": list(relationships),
            }
        ],
    }


def _dataset(name):
    return {"name": name, "source": f"warehouse.marts.{name}", "fields": []}


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


def test_list_documents_is_recursive_and_sorted(tmp_path):
    _write(tmp_path / "osi" / "b.json", {})
    _write(tmp_path / "osi" / "sub" / "a.json", {})
    (tmp_path / "osi" / ".DS_Store").write_bytes(b"\x00")
    (tmp_path / "osi" / "metrics.yml").write_text("metrics: []")
    assert [p.name for p in list_documents(tmp_path / "osi")] == ["b.json", "metrics.yml", "a.json"]
    assert list_documents(tmp_path / "missing") == []


def test_load_refuses_non_json_file(tmp_path):
    path = tmp_path / "metrics.yml"
    path.write_text("metrics: []")
    with pytest.raises(ModelError) as exc:
        load_document(path)
    issue = exc.value.issues[0]
    assert issue.code == "not_json_document" and issue.document == str(path)


def test_load_refuses_invalid_json_and_non_object(tmp_path):
    bad = _write(tmp_path / "bad.json", {})
    bad.write_text("{nope")
    with pytest.raises(ModelError) as exc:
        load_document(bad)
    assert exc.value.issues[0].code == "invalid_json"
    arr = _write(tmp_path / "arr.json", {})
    arr.write_text("[]")
    with pytest.raises(ModelError) as exc:
        load_document(arr)
    assert exc.value.issues[0].code == "invalid_document"


def test_merge_folds_documents_by_name(tmp_path):
    a = load_document(
        _write(
            tmp_path / "datasets.json",
            _doc("p", datasets=[_dataset("orders"), _dataset("invoices")]),
        )
    )
    b = load_document(
        _write(
            tmp_path / "metrics.json",
            _doc("p_metrics", metrics=[{"name": "total", "expression": {"dialects": []}}]),
        )
    )
    model = merge([a, b])
    assert sorted(model.datasets) == ["invoices", "orders"]
    assert model.datasets["orders"][1].name == "datasets.json"
    assert list(model.metrics) == ["total"] and model.names == ["p", "p_metrics"]


def test_merge_rejects_duplicate_names_naming_both_documents(tmp_path):
    a = load_document(_write(tmp_path / "datasets.json", _doc("p", datasets=[_dataset("orders")])))
    b = load_document(_write(tmp_path / "extra.json", _doc("q", datasets=[_dataset("orders")])))
    with pytest.raises(ModelError) as exc:
        merge([a, b])
    issue = exc.value.issues[0]
    assert issue.code == "duplicate_name"
    assert "orders" in issue.message
    assert "datasets.json" in issue.message and "extra.json" in issue.message


def test_merge_rejects_wrong_version_and_missing_semantic_model(tmp_path):
    old = load_document(_write(tmp_path / "old.json", _doc("p", version="0.1.0")))
    with pytest.raises(ModelError) as exc:
        merge([old])
    assert exc.value.issues[0].code == "unsupported_version"
    flat = load_document(_write(tmp_path / "flat.json", {"version": "0.1.1", "datasets": []}))
    with pytest.raises(ModelError) as exc:
        merge([flat])
    assert exc.value.issues[0].code == "invalid_document"
