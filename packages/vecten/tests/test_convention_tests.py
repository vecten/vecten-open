import json
from pathlib import Path

import pytest
import yaml

from vecten_platform.dbt import runner
from vecten_shared.config import AreaDecl, load_config, save_config
from vecten_shared.osi.generate import dataset_database, generate_datasets

from .docker_util import docker_available
from .fixture_project import build_fixture, render_fixture

CONVENTION_TESTS = (
    "convention_model_naming",
    "convention_descriptions",
    "convention_core_primary_key",
    "convention_staging_load_metadata",
    "convention_no_grants",
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


def _configure(path: Path, config: str) -> None:
    path.write_text("{{ config(" + config + ") }}\n" + path.read_text())


def _declare(root: Path, *areas: str) -> None:
    config = load_config(root)
    declared = {name: AreaDecl() for name in areas}
    save_config(root, config.model_copy(update={"areas": declared}))


def _into_area(root: Path, model: str, schema: str, *declared: str) -> None:
    _declare(root, *declared)
    _configure(root / model, f"schema='{schema}'")


CORE_YML = "dbt/models/core/schema.yml"
MART = "dbt/models/marts/company_overview.sql"
CORE_DEAL = "dbt/models/core/deal.sql"

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
    (
        "undeclared_area",
        "convention_model_naming",
        lambda d: _into_area(d, MART, "marts_fiance", "finance"),
        "model company_overview: lands in schema marts_fiance, an area vecten.toml does not "
        "declare; add [areas.fiance]",
    ),
    (
        "area_core_without_key",
        "convention_core_primary_key",
        lambda d: (
            _into_area(d, CORE_DEAL, "core_finance", "finance"),
            _drop_key(d / CORE_YML, "deal", "deal_id", "data_tests"),
        ),
        "model deal: declares no primary key column with unique and not_null tests",
    ),
    (
        "area_mart_without_description",
        "convention_descriptions",
        lambda d: (
            _into_area(d, MART, "marts_finance", "finance"),
            _drop_key(
                d / "dbt/models/marts/schema.yml", "company_overview", "country", "description"
            ),
        ),
        "model company_overview: column country has no description",
    ),
    (
        "schema_outside_the_convention",
        "convention_model_naming",
        lambda d: _configure(d / MART, "schema='marts_fin_ops'"),
        "model company_overview: lands in schema marts_fin_ops; the convention allows staging, "
        "snapshots, core, marts and the core_<area> and marts_<area> schemas of declared areas",
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


def test_models_in_declared_areas_pass_every_convention_test(tmp_path):
    root = render_fixture(tmp_path, "snowflake")
    _into_area(root, MART, "marts_finance", "finance", "payroll")
    _configure(root / CORE_DEAL, "schema='core_finance'")
    runs = build_fixture(root, docs=True)
    assert [r.returncode for r in runs] == [0, 0, 0], runner.failed_nodes(root / "dbt")
    target = root / "dbt" / "target"
    config = load_config(root)
    doc = generate_datasets(
        json.loads((target / "manifest.json").read_text()),
        json.loads((target / "catalog.json").read_text()),
        database=dataset_database(config),
        model_name="acme",
        areas=config.areas,
    )
    sources = {d["name"]: d["source"] for d in doc["semantic_model"][0]["datasets"]}
    assert sources == {
        "company_overview": "acme.marts_finance.company_overview",
        "sales_pipeline": "acme.marts.sales_pipeline",
    }


def _seed_with_grants(root: Path) -> None:
    seeds = root / "dbt" / "seeds"
    seeds.mkdir(exist_ok=True)
    (seeds / "country_names.csv").write_text("code,name\nDE,Germany\n")
    (seeds / "schema.yml").write_text(
        "version: 2\nseeds:\n  - name: country_names\n    config:\n      grants: {select: []}\n"
    )


def _package_with_grants(root: Path) -> None:
    project = root / "dbt" / "dbt_project.yml"
    project.write_text(
        project.read_text().replace(
            "\nmodels:\n", "\nmodels:\n  vecten_convention:\n    +grants: {select: []}\n", 1
        )
    )


SNAPSHOT = "dbt/snapshots/snapshot_demo_deal.sql"

# (id, mutation, violation line): each fails the run before dbt builds anything.
# An empty grants list issues no GRANT on a first build, so a slip of the gate would build
# green on the emulation, which refuses dbt's GRANT statements.
GATE_MUTATIONS = [
    (
        "mart_grants",
        lambda d: _configure(d / MART, "grants={'select': []}"),
        "model company_overview: sets dbt grants; read grants belong to the MCP",
    ),
    ("seed_grants", _seed_with_grants, "seed country_names: sets dbt grants"),
    (
        "snapshot_grants",
        lambda d: (d / SNAPSHOT).write_text(
            (d / SNAPSHOT)
            .read_text()
            .replace("schema='snapshots',", "schema='snapshots',\n    grants={'select': []},")
        ),
        "snapshot snapshot_demo_deal: sets dbt grants",
    ),
    (
        "package_model_grants",
        _package_with_grants,
        "model vecten_convention.metricflow_time_spine: sets dbt grants; read grants belong to "
        "the MCP; override it with +grants: {} under models: vecten_convention: in "
        "dbt/dbt_project.yml",
    ),
    (
        "copy_grants_off",
        lambda d: _configure(d / MART, "copy_grants=false"),
        "model company_overview: sets copy_grants to false",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "violation"),
    [case[1:] for case in GATE_MUTATIONS],
    ids=[case[0] for case in GATE_MUTATIONS],
)
def test_grants_fail_the_run_before_anything_is_built(tmp_path, mutate, violation):
    root = render_fixture(tmp_path, "snowflake")
    mutate(root)
    runs = build_fixture(root)
    assert [r.returncode for r in runs] == [1]
    output = runs[0].stdout + runs[0].stderr
    assert "convention_no_grants failed before the build" in output and violation in output
    assert not (root / "dbt" / "target" / "run_results.json").exists()


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="docker not reachable")
def test_copy_grants_false_passes_on_postgres(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    _configure(root / MART, "copy_grants=false")
    runs = build_fixture(root)
    assert [r.returncode for r in runs] == [0, 0], runner.failed_nodes(root / "dbt")
