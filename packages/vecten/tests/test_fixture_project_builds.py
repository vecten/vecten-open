import json
from pathlib import Path

import pytest

from vecten_platform.dbt import runner
from vecten_platform.dbt.local_warehouse import local_target
from vecten_shared.config import load_config
from vecten_shared.osi.generate import dataset_database, generate_datasets, render_document

from .docker_util import docker_available
from .fixture_project import build_fixture, render_fixture

SHARED_FIXTURES = Path(__file__).resolve().parents[2] / "vecten-shared" / "tests" / "fixtures"
GOLDEN = SHARED_FIXTURES / "osi" / "datasets.json"

EXPECTED = {
    ("staging", "staging_demo_company"),
    ("staging", "staging_demo_deal"),
    ("snapshots", "snapshot_demo_company"),
    ("snapshots", "snapshot_demo_deal"),
    ("core", "company"),
    ("core", "deal"),
    ("core", "metricflow_time_spine"),
    ("marts", "sales_pipeline"),
    ("marts", "company_overview"),
}


def build(root, inside=None, validate=False):
    """Replay, build and generate docs; `inside(target)` runs while the warehouse is up."""
    runs = build_fixture(root, docs=True, validate=validate, inside=inside)
    assert [r.returncode for r in runs] == [0] * (4 if validate else 3), (
        runner.failed_nodes(root / "dbt"),
        [r.stderr for r in runs],
    )
    manifest = json.loads((root / "dbt" / "target" / "manifest.json").read_text())
    built = {
        (n["schema"].lower(), n["alias"].lower())
        for n in manifest["nodes"].values()
        if n["resource_type"] in ("model", "snapshot")
    }
    return built, manifest


def _mart_rows(target) -> dict[str, tuple]:
    sql = "select name, email, domain, deal_count from marts.company_overview"
    if target.kind == "snowflake":
        import snowflake.connector

        conn = snowflake.connector.connect(
            account="fakesnow",
            user="u",
            password="p",
            database=target.spec.database,
            schema="MARTS",
        )
        rows = conn.cursor().execute(sql).fetchall()
    else:
        import psycopg

        with psycopg.connect(
            host=target.spec.host,
            port=target.spec.port,
            user=target.spec.role,
            password=target.password,
            dbname=target.spec.database,
        ) as conn:
            rows = conn.execute(sql).fetchall()
    return {name: tuple(rest) for name, *rest in rows}


def _assert_identity_macros(rows: dict[str, tuple]) -> None:
    assert rows["Acme GmbH"] == ("sales@acme.com", "acme.com", 2)
    assert rows["Beta Ltd"] == ("hello@beta.co.uk", "beta.co.uk", 1)
    assert rows["Gamma SAS"] == (None, "gamma.fr", 1)


def test_fixture_builds_on_the_snowflake_emulation(tmp_path):
    seen = {}
    built, manifest = build(
        render_fixture(tmp_path, "snowflake"),
        inside=lambda t: seen.update(_mart_rows(t)),
        validate=True,
    )
    assert built == EXPECTED
    metrics = {k.rsplit(".", 1)[1]: m for k, m in manifest["metrics"].items()}
    assert set(metrics) == {"total_pipeline", "deal_count", "company_total"}
    homes = {
        name: m["type_params"]["metric_aggregation_params"]["semantic_model"]
        for name, m in metrics.items()
    }
    assert homes == {
        "total_pipeline": "sales_pipeline",
        "deal_count": "sales_pipeline",
        "company_total": "company_overview",
    }
    assert (tmp_path / "proj" / "dbt" / "target" / "catalog.json").is_file()
    _assert_identity_macros(seen)


def test_fixture_metrics_name_their_dataset_in_every_expression(tmp_path):
    import jsonschema

    from vecten_shared.osi import load_schema
    from vecten_shared.osi.documents import list_documents, load_document, merge

    root = render_fixture(tmp_path, "snowflake")
    jsonschema.validate(json.loads((root / "osi" / "datasets.json").read_text()), load_schema())
    model = merge([load_document(p) for p in list_documents(root / "osi")])
    assert set(model.metrics) == {"total_pipeline", "deal_count", "company_total"}
    for metric, _ in model.metrics.values():
        expression = metric["expression"]["dialects"][0]["expression"]
        dataset = expression.split("(", 1)[1].split(".", 1)[0]
        assert dataset in model.datasets, (metric["name"], expression)


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="docker not reachable")
def test_fixture_builds_on_postgres_with_the_same_names(tmp_path):
    seen = {}
    built, _ = build(
        render_fixture(tmp_path, "postgres"), inside=lambda t: seen.update(_mart_rows(t))
    )
    assert built == EXPECTED
    assert (tmp_path / "proj" / "dbt" / "target" / "catalog.json").is_file()
    _assert_identity_macros(seen)


def test_a_failing_build_is_returned_not_raised(tmp_path):
    root = render_fixture(tmp_path, "snowflake")
    deal = root / "dbt" / "models" / "core" / "deal.sql"
    deal.write_text(deal.read_text().replace("is_won", "is_lost"))
    runs = build_fixture(root, docs=True)
    assert [r.returncode for r in runs] == [1]
    assert "deal" in {f["name"] for f in runner.failed_nodes(root / "dbt")}


def _generate(root, database="warehouse"):
    target = root / "dbt" / "target"
    manifest = json.loads((target / "manifest.json").read_text())
    catalog = json.loads((target / "catalog.json").read_text())
    return render_document(
        generate_datasets(manifest, catalog, database=database, model_name="acme")
    )


def test_fixture_overlay_carries_the_golden_and_the_shared_metrics():
    overlay = Path(__file__).parent / "fixture_project" / "overlay" / "osi"
    assert (overlay / "datasets.json").read_text() == GOLDEN.read_text()
    shared_metrics = SHARED_FIXTURES / "osi" / "metrics.json"
    assert (overlay / "metrics.json").read_text() == shared_metrics.read_text()


def test_emulation_build_generates_the_golden_and_dbt_parses_it(tmp_path):
    root = render_fixture(tmp_path, "snowflake")
    build(root)
    text = _generate(root, dataset_database(load_config(root)))
    # The golden names the Postgres build database; on Snowflake the project's stands in.
    assert text == GOLDEN.read_text().replace('"warehouse.', '"acme.')
    (root / "osi" / "datasets.json").write_text(text)
    with local_target(load_config(root), root / ".vecten" / "dbt-local") as target:
        parsed = runner.invoke_dbt(
            ["parse"], project_dir=root / "dbt", env=runner.base_env(root / "dbt", target.env)
        )
        assert parsed.returncode == 0, parsed.stderr
    manifest = json.loads((root / "dbt" / "target" / "manifest.json").read_text())
    assert {k.rsplit(".", 1)[1] for k in manifest["semantic_models"]} == {
        "sales_pipeline",
        "company_overview",
    }
    assert {k.rsplit(".", 1)[1] for k in manifest["metrics"]} == {
        "total_pipeline",
        "deal_count",
        "company_total",
    }


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="docker not reachable")
def test_postgres_build_generates_the_same_document(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    build(root)
    assert _generate(root) == GOLDEN.read_text()
