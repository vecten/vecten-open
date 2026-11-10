import hashlib
import json

import yaml

from vecten.next.existing_warehouse import existing_warehouse_items
from vecten.next.items import Item
from vecten.next.modelling import modelling_items
from vecten.next.semantic import osi_digest, semantic_items
from vecten.next.stack import stack_items
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config, save_config
from vecten_shared.records import remove_record, write_record

from .fixture_project import render_fixture

POSTGRES_URL = "postgres://dw.internal:5432/analytics"
SNOWFLAKE_URL = "snowflake://ab12345.eu-central-1/analytics"
VALIDATE = Item("skill", "Validate the semantic model", "semantic")
NO_METRICS = Item("skill", "No metrics defined in osi/metrics.json", "semantic")
BUILD = Item("command", "Build the dbt project locally", "vecten dbt-build-local")
STALE = Item(
    "command", "osi/datasets.json is stale against the last local build", "vecten dbt-build-local"
)
ADMIN_SECRET = Item(
    "command",
    "Store the warehouse admin credential",
    "vecten secret set WAREHOUSE_ADMIN_PASSWORD",
)
INTROSPECT = Item("command", "Draft osi/datasets.json from the warehouse", "vecten mcp introspect")
REFINE = Item("skill", "Refine the drafted datasets", "semantic")
PROVISIONER_SCRIPT = Item(
    "command", "Prepare the existing warehouse for the MCP", "vecten mcp provisioner-script"
)
LOCAL_PROVISIONER_SECRETS = {
    POSTGRES_URL: Item(
        "command",
        "Store the provisioner credentials locally",
        "vecten secret set MCP_PROVISIONER_PASSWORD && vecten secret set MCP_IMPERSONATOR_PASSWORD",
    ),
    SNOWFLAKE_URL: Item(
        "command",
        "Store the provisioner credentials locally",
        "vecten secret set MCP_PROVISIONER_PRIVATE_KEY",
    ),
}
LOCAL_PROVISIONER_ENV = {
    POSTGRES_URL: "MCP_PROVISIONER_PASSWORD=p\nMCP_IMPERSONATOR_PASSWORD=i\n",
    SNOWFLAKE_URL: "MCP_PROVISIONER_PRIVATE_KEY=k\n",
}


def render_mcp_only(tmp_path, url=POSTGRES_URL):
    root = tmp_path / "proj"
    answers = {"name": "acme", "users": 3, "has_warehouse": "yes", "warehouse_url": url}
    render_project(root, ScaffoldParams.from_answers(answers, {}))
    return root


def green_build(root):
    sha = hashlib.sha256((root / "osi" / "datasets.json").read_bytes()).hexdigest()
    write_record(root, "dbt-build-local", {"ok": True, "datasets_sha256": sha})


def validated(root, ok=True):
    digest = osi_digest(root / "osi")
    write_record(root, "mcp-validate", {"ok": ok, "errors": 0, "warnings": 0, "osi_sha256": digest})


def edit_yaml(path, change):
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def model_entry(doc, name):
    return next(m for m in doc["models"] if m["name"] == name)


def column_entry(model, name):
    return next(c for c in model["columns"] if c["name"] == name)


# Stack


def test_stack_item_until_up_or_down_is_recorded(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    config = load_config(root)
    assert stack_items(root, config) == [Item("command", "Start the local stack", "vecten up")]
    write_record(root, "up", {"profiles": []})
    assert stack_items(root, config) == []
    remove_record(root, "up")
    write_record(root, "down", {"profiles": [], "volumes_removed": False})
    assert stack_items(root, config) == []


def test_an_existing_snowflake_warehouse_has_no_local_stack(tmp_path):
    root = render_mcp_only(tmp_path, SNOWFLAKE_URL)
    assert stack_items(root, load_config(root)) == []


# Modelling


def test_fresh_fixture_reports_only_the_build(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    assert modelling_items(root, load_config(root)) == [BUILD]


def test_staging_model_without_snapshot(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    (root / "dbt" / "snapshots" / "snapshot_demo_deal.sql").unlink()
    items = modelling_items(root, load_config(root))
    assert items[0] == Item("skill", "Snapshot staging_demo_deal", "model")


def test_snapshot_no_core_model_reads(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    (root / "dbt" / "models" / "core" / "deal.sql").unlink()
    items = modelling_items(root, load_config(root))
    assert items[0] == Item("skill", "No core model reads snapshot_demo_deal", "model")


def test_double_quoted_ref_counts_as_a_read(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    deal = root / "dbt" / "models" / "core" / "deal.sql"
    deal.write_text(
        deal.read_text().replace("ref('snapshot_demo_deal')", 'ref("snapshot_demo_deal")')
    )
    assert modelling_items(root, load_config(root)) == [BUILD]


def test_blank_column_description_names_the_first_column(tmp_path):
    root = render_fixture(tmp_path, "postgres")

    def blank(doc):
        company = model_entry(doc, "company")
        column_entry(company, "country")["description"] = ""
        del column_entry(company, "created_at")["description"]

    edit_yaml(root / "dbt" / "models" / "core" / "schema.yml", blank)
    items = modelling_items(root, load_config(root))
    assert items == [Item("skill", "Describe company (column country)", "model"), BUILD]


def test_model_without_entry_or_description(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    (root / "dbt" / "models" / "core" / "extra.sql").write_text("select 1 as id\n")
    edit_yaml(
        root / "dbt" / "models" / "marts" / "schema.yml",
        lambda doc: model_entry(doc, "sales_pipeline").pop("description"),
    )
    items = modelling_items(root, load_config(root))
    assert items == [
        Item("skill", "Describe extra", "model"),
        Item("skill", "Describe sales_pipeline", "model"),
        BUILD,
    ]


def test_modelling_item_order(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    dbt = root / "dbt"
    (dbt / "models" / "staging" / "staging_demo_company.sql").unlink()
    (dbt / "snapshots" / "snapshot_demo_deal.sql").unlink()
    (dbt / "models" / "core" / "company.sql").unlink()
    edit_yaml(
        dbt / "models" / "marts" / "schema.yml",
        lambda doc: column_entry(model_entry(doc, "sales_pipeline"), "stage").update(
            description=" "
        ),
    )
    titles = [i.title for i in modelling_items(root, load_config(root))]
    assert titles == [
        "Model `company` from `raw_demo.company`",
        "Snapshot staging_demo_deal",
        "No core model reads snapshot_demo_company",
        "Describe sales_pipeline (column stage)",
        "Build the dbt project locally",
    ]


# Semantic, platform projects


def test_platform_semantic_without_build_record_reports_validate_only(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    assert semantic_items(root, load_config(root)) == [VALIDATE]


def test_platform_metrics_file_without_metrics(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    config = load_config(root)
    metrics = root / "osi" / "metrics.json"
    doc = json.loads(metrics.read_text())
    doc["semantic_model"][0]["metrics"] = []
    metrics.write_text(json.dumps(doc))
    assert semantic_items(root, config) == [NO_METRICS, VALIDATE]
    metrics.unlink()
    assert semantic_items(root, config) == [NO_METRICS, VALIDATE]
    metrics.write_text("{not json")
    assert semantic_items(root, config) == [NO_METRICS, VALIDATE]


def test_platform_start_empty_asks_for_no_metric_without_a_dataset(tmp_path):
    root = tmp_path / "proj"
    answers = {
        "name": "sandbox",
        "users": 2,
        "has_warehouse": "no",
        "sources": [],
        "warehouse": "postgres",
    }
    render_project(root, ScaffoldParams.from_answers(answers, {}))
    config = load_config(root)
    assert semantic_items(root, config) == [VALIDATE]
    validated(root)
    assert semantic_items(root, config) == []


def test_platform_validate_record_matching_failed_and_stale(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    config = load_config(root)
    validated(root)
    assert semantic_items(root, config) == []
    validated(root, ok=False)
    assert semantic_items(root, config) == [VALIDATE]
    validated(root)
    metrics = root / "osi" / "metrics.json"
    metrics.write_text(metrics.read_text() + "\n")
    assert semantic_items(root, config) == [VALIDATE]


def test_stale_datasets_move_from_modelling_to_semantic(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    config = load_config(root)
    green_build(root)
    validated(root)
    assert modelling_items(root, config) == []
    assert semantic_items(root, config) == []
    datasets = root / "osi" / "datasets.json"
    datasets.write_text(datasets.read_text() + "\n")
    assert modelling_items(root, config) == []
    assert semantic_items(root, config) == [STALE, VALIDATE]


# Semantic, MCP-only projects


def test_mcp_only_semantic_order(tmp_path):
    root = render_mcp_only(tmp_path)
    config = load_config(root)
    assert not config.has("platform")
    assert semantic_items(root, config) == [ADMIN_SECRET, INTROSPECT, REFINE, NO_METRICS, VALIDATE]
    (root / ".env").write_text("WAREHOUSE_ADMIN_PASSWORD=secret\n")
    assert semantic_items(root, config) == [INTROSPECT, REFINE, NO_METRICS, VALIDATE]


def test_snowflake_semantic_order_has_no_admin_password(tmp_path):
    # Introspection signs in to Snowflake with a key pair it generates on first use.
    root = render_mcp_only(tmp_path, SNOWFLAKE_URL)
    assert semantic_items(root, load_config(root)) == [INTROSPECT, REFINE, NO_METRICS, VALIDATE]


def test_mcp_only_with_datasets_and_metrics(tmp_path):
    root = render_mcp_only(tmp_path)
    (root / ".env").write_text("WAREHOUSE_ADMIN_PASSWORD=secret\n")
    fixture = render_fixture(tmp_path / "platform", "postgres")
    for name in ("datasets.json", "metrics.json"):
        (root / "osi" / name).write_text((fixture / "osi" / name).read_text())
    config = load_config(root)
    assert semantic_items(root, config) == [VALIDATE]
    validated(root)
    assert semantic_items(root, config) == []


def test_drafted_datasets_keep_the_refine_item_until_described_and_keyed(tmp_path):
    root = render_mcp_only(tmp_path)
    (root / ".env").write_text("WAREHOUSE_ADMIN_PASSWORD=secret\n")
    config = load_config(root)
    dataset = {
        "name": "orders",
        "source": "db.core.orders",
        "description": "",
        "fields": [{"name": "id", "expression": {"dialects": []}}],
    }

    def draft():
        doc = {"version": "0.1.1", "semantic_model": [{"name": "m", "datasets": [dataset]}]}
        (root / "osi" / "datasets.json").write_text(json.dumps(doc))
        return semantic_items(root, config)

    assert draft() == [REFINE, NO_METRICS, VALIDATE]
    dataset["description"] = "   "
    assert draft() == [REFINE, NO_METRICS, VALIDATE]
    dataset["description"] = "Orders placed"
    assert draft() == [REFINE, NO_METRICS, VALIDATE]
    dataset["primary_key"] = ["id"]
    assert draft() == [NO_METRICS, VALIDATE]


def test_mcp_only_unparsable_datasets_count_as_none(tmp_path):
    root = render_mcp_only(tmp_path)
    (root / ".env").write_text("WAREHOUSE_ADMIN_PASSWORD=secret\n")
    (root / "osi" / "datasets.json").write_text("[")
    items = semantic_items(root, load_config(root))
    assert items[:2] == [INTROSPECT, REFINE]


# Existing warehouse


def test_existing_warehouse_items_store_the_credentials_then_run_the_script(tmp_path):
    url = POSTGRES_URL
    root = render_mcp_only(tmp_path, url)
    items = existing_warehouse_items(root, load_config(root), "prod")
    assert items == [LOCAL_PROVISIONER_SECRETS[url], PROVISIONER_SCRIPT]
    (root / ".env").write_text(LOCAL_PROVISIONER_ENV[url])
    assert existing_warehouse_items(root, load_config(root), "prod") == [PROVISIONER_SCRIPT]
    write_record(root, "provisioner-local", {"confirmed": True})
    assert existing_warehouse_items(root, load_config(root), "prod") == []


def test_snowflake_script_waits_for_the_deploy_layer(tmp_path):
    # The Snowflake script trusts the deployed MCP's issuer, which the deploy layer names, and
    # generates the provisioner's key itself, so nothing is asked before it.
    root = render_mcp_only(tmp_path, SNOWFLAKE_URL)
    assert existing_warehouse_items(root, load_config(root), "prod") == []


def test_a_prod_run_of_the_script_clears_it(tmp_path):
    root = render_mcp_only(tmp_path)
    (root / ".env").write_text(LOCAL_PROVISIONER_ENV[POSTGRES_URL])
    config = load_config(root)
    config.environments["prod"].provisioner_script_run = True
    save_config(root, config)
    assert existing_warehouse_items(root, load_config(root), "prod") == []


def test_platform_project_has_no_existing_warehouse_items(tmp_path):
    root = render_fixture(tmp_path, "postgres")
    assert existing_warehouse_items(root, load_config(root), "prod") == []
