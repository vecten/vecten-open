"""The project of the "Refunds" scenario, shared with the evaluation harness: refunds land in
raw and core while the `sales_pipeline` mart and the `total_revenue` metric leave them out,
and the two halves of the `semantic` skill's fix."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from vecten_shared.config import load_config, save_config
from vecten_shared.osi.evals import sql_sha256
from vecten_shared.osi.semantic import SemanticModel

from ..fixture_project import RAW

QUESTION = "What is our revenue by country?"
REQUEST = {"metrics": ["total_revenue"], "dimensions": ["sales_pipeline.country"]}
REFUNDS = {10: 200.5}
REFUNDED_SYNONYMS = ["refunds", "refunded"]
REFUNDED_COLUMN = (
    "      - name: refunded_amount\n"
    "        description: Amount refunded on the deal in the source currency.\n"
    "        config:\n"
    f"          meta: {{osi: {{synonyms: [{', '.join(REFUNDED_SYNONYMS)}]}}}}\n"
)
CREATED_AT = "      - name: created_at\n        description: When the deal was created"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1))


def project_at_a(root: Path) -> None:
    """The deployed project: refunds land in raw and core; the mart and the metric omit them."""
    deals = [{**row, "refunded_amount": REFUNDS.get(row["id"], 0)} for row in RAW["deal"]]
    (root / "connectors/demo/fixtures/deal.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in deals)
    )
    dbt = root / "dbt"
    edit(
        dbt / "models/staging/staging_demo_deal.sql",
        "    cast(amount as decimal(18, 2)) as amount,\n",
        "    cast(amount as decimal(18, 2)) as amount,\n"
        "    cast(refunded_amount as decimal(18, 2)) as refunded_amount,\n",
    )
    edit(dbt / "snapshots/snapshot_demo_deal.sql", "'amount', ", "'amount', 'refunded_amount', ")
    edit(dbt / "models/core/deal.sql", "    amount,\n", "    amount,\n    refunded_amount,\n")
    edit(dbt / "models/core/schema.yml", CREATED_AT, REFUNDED_COLUMN + CREATED_AT)
    metrics = json.loads((root / "osi/metrics.json").read_text())
    metrics["semantic_model"][0]["metrics"].append(
        {
            "name": "total_revenue",
            "description": "Revenue from deals.",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(sales_pipeline.amount)"}]
            },
        }
    )
    (root / "osi/metrics.json").write_text(json.dumps(metrics, indent=2))
    config = load_config(root)
    save_config(
        root,
        config.model_copy(
            update={"mcp": config.mcp.model_copy(update={"admins": ["ada@acme.test"]})}
        ),
    )


def add_refunds_to_the_mart(root: Path) -> None:
    """The model half of the fix: `refunded_amount` in the mart and in its dataset."""
    edit(
        root / "dbt/models/marts/sales_pipeline.sql",
        "    d.amount,\n",
        "    d.amount,\n    d.refunded_amount,\n",
    )
    edit(root / "dbt/models/marts/schema.yml", CREATED_AT, REFUNDED_COLUMN + CREATED_AT)
    datasets = json.loads((root / "osi/datasets.json").read_text())
    pipeline = next(
        d for d in datasets["semantic_model"][0]["datasets"] if d["name"] == "sales_pipeline"
    )
    amount = next(f for f in pipeline["fields"] if f["name"] == "amount")
    refunded = json.loads(json.dumps(amount).replace('"amount"', '"refunded_amount"'))
    refunded["description"] = "Amount refunded on the deal in the source currency."
    # The same synonyms as the mart column, which a `vecten dbt-build-local` rebuild generates.
    refunded["ai_context"] = {"synonyms": REFUNDED_SYNONYMS}
    pipeline["fields"].insert(pipeline["fields"].index(amount) + 1, refunded)
    (root / "osi/datasets.json").write_text(json.dumps(datasets, indent=2))


def fix_the_metric(root: Path, gap: str) -> None:
    """The semantic half of the fix: the metric less refunds, and the eval case naming the gap."""
    metrics = json.loads((root / "osi/metrics.json").read_text())
    metric = next(
        m for m in metrics["semantic_model"][0]["metrics"] if m["name"] == "total_revenue"
    )
    metric["description"] = "Revenue from deals less recorded refunds."
    metric["expression"]["dialects"][0]["expression"] = (
        "SUM(sales_pipeline.amount) - SUM(sales_pipeline.refunded_amount)"
    )
    metric["ai_context"] = {"examples": [QUESTION]}
    (root / "osi/metrics.json").write_text(json.dumps(metrics, indent=2))
    compiled = SemanticModel.load(root / "osi", kind="postgres").compile(REQUEST)
    case = {
        "name": "revenue-excludes-refunds",
        "gap": gap,
        "question": QUESTION,
        "request": REQUEST,
        "expect": {"outcome": "compiled", "sql_sha256": sql_sha256(compiled["sql"])},
    }
    (root / "evals/semantic.yaml").write_text(yaml.safe_dump({"cases": [case]}))


def semantic_fix(root: Path, gap: str) -> None:
    """What the `semantic` skill writes for the gap: case (b), the mart column, then the metric."""
    add_refunds_to_the_mart(root)
    fix_the_metric(root, gap)
