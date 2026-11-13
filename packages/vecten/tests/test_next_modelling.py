import hashlib

import yaml

from vecten.next.modelling import modelling_items
from vecten_shared.config import AreaDecl, load_config, save_config
from vecten_shared.records import write_record

from .fixture_project import render_fixture


def _green_build(root):
    text = (root / "osi" / "datasets.json").read_text()
    sha = hashlib.sha256(text.encode()).hexdigest()
    write_record(root, "dbt-build-local", {"ok": True, "datasets_sha256": sha})
    return text


def test_items_when_raw_tables_have_no_staging_model(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    (root / "dbt" / "models" / "staging" / "staging_demo_deal.sql").unlink()
    items = modelling_items(root, load_config(root))
    assert items[0].kind == "skill" and items[0].resolution == "model"
    assert items[0].title == "Model `deal` from `raw_demo.deal`"
    assert [i.kind for i in items].count("skill") == 1


def test_item_when_no_green_build_record(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    items = modelling_items(root, load_config(root))
    assert [i.kind for i in items] == ["command"]
    assert items[0].resolution == "vecten dbt-build-local"
    write_record(root, "dbt-build-local", {"ok": False})
    assert [i.resolution for i in modelling_items(root, load_config(root))] == [
        "vecten dbt-build-local"
    ]


def test_green_build_leaves_stale_datasets_to_the_semantic_items(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    text = _green_build(root)
    assert modelling_items(root, load_config(root)) == []
    (root / "osi" / "datasets.json").write_text(text + "\n")
    assert modelling_items(root, load_config(root)) == []


def test_no_items_without_the_platform_component(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    config = load_config(root)
    assert modelling_items(root, config.model_copy(update={"components": ["mcp"]})) == []


def _move(root, layer, model, area_layer):
    """Move a model and its schema.yml entry to `models/<area_layer>/`, as an area's folder."""
    source = root / "dbt" / "models" / layer
    target = root / "dbt" / "models" / area_layer
    target.mkdir(exist_ok=True)
    (source / f"{model}.sql").rename(target / f"{model}.sql")
    doc = yaml.safe_load((source / "schema.yml").read_text())
    entry = next(m for m in doc["models"] if m["name"] == model)
    doc["models"].remove(entry)
    (source / "schema.yml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return entry, target


def test_models_in_a_declared_areas_folders_must_be_described(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    _green_build(root)
    config = load_config(root)
    save_config(root, config.model_copy(update={"areas": {"finance": AreaDecl()}}))
    entry, mart_dir = _move(root, "marts", "company_overview", "marts_finance")
    _, core_dir = _move(root, "core", "deal", "core_finance")
    items = modelling_items(root, load_config(root))
    assert [i.title for i in items] == ["Describe deal", "Describe company_overview"]
    (mart_dir / "schema.yml").write_text(yaml.safe_dump({"version": 2, "models": [entry]}))
    assert [i.title for i in modelling_items(root, load_config(root))] == ["Describe deal"]


def test_a_snapshot_read_only_by_an_area_core_model_is_read(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    _green_build(root)
    config = load_config(root)
    save_config(root, config.model_copy(update={"areas": {"finance": AreaDecl()}}))
    entry, core_dir = _move(root, "core", "deal", "core_finance")
    (core_dir / "schema.yml").write_text(yaml.safe_dump({"version": 2, "models": [entry]}))
    assert modelling_items(root, load_config(root)) == []
