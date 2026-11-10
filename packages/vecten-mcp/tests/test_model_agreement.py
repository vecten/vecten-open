"""`vecten mcp validate` and the server's loader reach the same verdict on every fixture."""

import json
import shutil
from pathlib import Path

import pytest

from vecten_mcp.model import ModelService
from vecten_mcp.state import StateStore
from vecten_shared.osi.extensions import vecten_extension
from vecten_shared.osi.validate import validate_osi

GOLDEN = Path(__file__).parents[2] / "vecten-shared" / "tests" / "fixtures" / "osi"


def _edit(name):
    def apply(osi, change):
        path = osi / name
        doc = json.loads(path.read_text())
        change(doc["semantic_model"][0], doc)
        path.write_text(json.dumps(doc))

    return apply


datasets, metrics = _edit("datasets.json"), _edit("metrics.json")


def _second_document(osi):
    first = json.loads((osi / "datasets.json").read_text())
    (osi / "extra.json").write_text(json.dumps(first))


def _expr(text):
    return {"dialects": [{"dialect": "ANSI_SQL", "expression": text}]}


def _metric_expression(text):
    return lambda osi: metrics(osi, lambda sm, doc: sm["metrics"][0].update(expression=_expr(text)))


EDITS = {
    "golden": lambda osi: None,
    "duplicate_dataset": _second_document,
    "schema_error": lambda osi: datasets(osi, lambda sm, doc: sm["datasets"][0].pop("source")),
    "wrong_version": lambda osi: datasets(osi, lambda sm, doc: doc.update(version="0.1.0")),
    "ambiguous_home": lambda osi: metrics(
        osi,
        lambda sm, doc: sm["metrics"][0].update(
            expression=_expr(
                "SUM(" + ")+SUM(".join(f"{d}.x" for d in _dataset_names(osi)[:2]) + ")"
            )
        ),
    ),
    "dialect_missing": lambda osi: metrics(
        osi,
        lambda sm, doc: sm["metrics"][0].update(
            expression={"dialects": [{"dialect": "MDX", "expression": "[Measures].[x]"}]}
        ),
    ),
    "subquery": _metric_expression("(SELECT 1)"),
    "unknown_function": _metric_expression("PG_SLEEP(1)"),
    "duplicate_field": lambda osi: datasets(
        osi, lambda sm, doc: sm["datasets"][0]["fields"].append(sm["datasets"][0]["fields"][0])
    ),
    "bad_extension": lambda osi: metrics(
        osi,
        lambda sm, doc: sm["metrics"][0].update(
            custom_extensions=[vecten_extension({"additivity": "bogus"})]
        ),
    ),
}


def _dataset_names(osi):
    doc = json.loads((osi / "datasets.json").read_text())
    return [d["name"] for d in doc["semantic_model"][0]["datasets"]]


def _service(osi, kind, state=None):
    return ModelService(
        osi, state or StateStore("sqlite://"), kind=kind, source_exists=lambda table: True
    )


@pytest.mark.parametrize("kind", ["postgres", "snowflake"])
@pytest.mark.parametrize("case", sorted(EDITS))
def test_validator_and_server_agree(tmp_path, case, kind):
    osi = tmp_path / "osi"
    shutil.copytree(GOLDEN, osi)
    EDITS[case](osi)
    report = validate_osi(osi, warehouse_kind=kind)
    service = _service(osi, kind)
    assert service.reload() is report.ok
    assert (case == "golden") is report.ok
    if not report.ok:
        codes = {e["code"] for e in service.health()["errors"]}
        assert codes == {e.code for e in report.errors}


@pytest.mark.parametrize("case", sorted(set(EDITS) - {"golden"}))
def test_a_broken_bundle_keeps_the_valid_model(tmp_path, case):
    osi = tmp_path / "osi"
    shutil.copytree(GOLDEN, osi)
    service = _service(osi, "postgres")
    assert service.reload()
    live = service.current.commit
    EDITS[case](osi)
    assert not service.reload()
    assert service.current.commit == live
    assert service.health()["status"] == "error"
