from pathlib import Path

import pytest
import yaml

from vecten_platform.dbt import runner

from .fixture_project import build_fixture, render_fixture

CONVENTION_TESTS = (
    "convention_model_naming",
    "convention_descriptions",
    "convention_core_primary_key",
    "convention_staging_load_metadata",
)


def _rename_staging_company(root: Path) -> None:
    dbt = root / "dbt"
    (dbt / "models/staging/staging_demo_company.sql").rename(dbt / "models/staging/stg_demo_co.sql")
    snapshot = dbt / "snapshots/snapshot_demo_company.sql"
    snapshot.write_text(snapshot.read_text().replace("'staging_demo_company'", "'stg_demo_co'"))


def _model_columns(doc: dict, model: str) -> list[dict]:
    return next(m for m in doc["models"] if m["name"] == model)["columns"]


def _drop_key(path: Path, model: str, column: str, key: str) -> None:
    doc = yaml.safe_load(path.read_text())
    next(c for c in _model_columns(doc, model) if c["name"] == column).pop(key)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _drop_column(path: Path, model: str, column: str) -> None:
    doc = yaml.safe_load(path.read_text())
    columns = _model_columns(doc, model)
    columns.remove(next(c for c in columns if c["name"] == column))
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _strip_source(path: Path) -> None:
    path.write_text(path.read_text().replace("'demo' as _source", "'demo' as _origin"))


CORE_YML = "dbt/models/core/schema.yml"

# (id, failing convention test, mutation, violation line the message must carry)
MUTATIONS = [
    (
        "naming",
        "convention_model_naming",
        _rename_staging_company,
        "model stg_demo_co: name does not match the staging rule",
    ),
    (
        "missing_description",
        "convention_descriptions",
        lambda d: _drop_key(d / CORE_YML, "company", "country", "description"),
        "model company: column country has no description",
    ),
    (
        "undeclared_column",
        "convention_descriptions",
        lambda d: _drop_column(d / CORE_YML, "company", "country"),
        "model company: column country is not declared in schema.yml",
    ),
    (
        "primary_key",
        "convention_core_primary_key",
        lambda d: _drop_key(d / CORE_YML, "deal", "deal_id", "data_tests"),
        "model deal: declares no primary key column with unique and not_null tests",
    ),
    (
        "staging_load_metadata",
        "convention_staging_load_metadata",
        lambda d: _strip_source(d / "dbt/models/staging/staging_demo_deal.sql"),
        "model staging_demo_deal: lacks column _source",
    ),
]


def test_fixture_passes_every_convention_test(tmp_path):
    root = render_fixture(tmp_path, "snowflake")
    runs = build_fixture(root)
    assert [r.returncode for r in runs] == [0, 0], runner.failed_nodes(root / "dbt")
    results = (root / "dbt" / "target" / "run_results.json").read_text()
    for name in CONVENTION_TESTS:
        assert f"test.vecten_convention.{name}" in results


@pytest.mark.parametrize(
    ("name", "mutate", "violation"),
    [case[1:] for case in MUTATIONS],
    ids=[case[0] for case in MUTATIONS],
)
def test_mutated_fixture_fails_the_named_test(tmp_path, name, mutate, violation):
    root = render_fixture(tmp_path, "snowflake")
    mutate(root)
    runs = build_fixture(root)
    assert runs[0].returncode == 0, runner.failed_nodes(root / "dbt")
    assert runs[-1].returncode == 1
    failed = {f["name"]: f["message"] for f in runner.failed_nodes(root / "dbt")}
    assert set(failed) == {name}, failed
    assert violation in failed[name]
