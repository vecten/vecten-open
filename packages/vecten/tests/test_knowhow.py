import json
from importlib import resources

import pytest

from vecten.knowhow import GUIDES, load_costs, load_guide, skill_body


def test_guides_load_from_package_data():
    for name in ("guide", "cloud", "domain", "github"):
        text = load_guide(name)
        assert len(text) > 200, name


def test_guide_topic_is_the_guide_skill_body():
    assert "guide" in GUIDES
    assert load_guide("guide") == skill_body("guide")
    assert not resources.files("vecten.knowhow").joinpath("guide.md").is_file()


def test_costs_cover_every_cloud_tier_and_profile():
    costs = load_costs()
    assert costs.clouds == ["aws", "scaleway"] and costs.tiers == ["small", "medium", "large"]
    for cloud in costs.clouds:
        for tier in costs.tiers:
            full = costs.estimate(cloud, tier, ["platform", "mcp"], "standard")
            mcp_only = costs.estimate(cloud, tier, ["mcp"], "standard")
            assert mcp_only.monthly_usd < full.monthly_usd
            for comps in (["mcp"], ["platform"]):
                for warehouse in ("postgres", "snowflake"):
                    case = (cloud, tier, comps, warehouse)
                    standard = costs.estimate(cloud, tier, comps, "standard", warehouse)
                    minimal = costs.estimate(cloud, tier, comps, "minimal", warehouse)
                    assert minimal.monthly_usd <= standard.monthly_usd, case


@pytest.mark.parametrize("tier", ["small", "medium", "large"])
@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_minimal_profile_saves_with_both_components(cloud, tier):
    costs = load_costs()
    both = ["platform", "mcp"]
    for warehouse in ("postgres", "snowflake"):
        standard = costs.estimate(cloud, tier, both, "standard", warehouse)
        minimal = costs.estimate(cloud, tier, both, "minimal", warehouse)
        assert minimal.monthly_usd <= standard.monthly_usd, warehouse
        if warehouse == "postgres":
            assert minimal.monthly_usd < standard.monthly_usd


def test_minimal_single_component_vm_priced_as_standard():
    costs = load_costs()
    vm = costs.table["aws"]["vm"]
    platform = costs.estimate("aws", "medium", ["platform"], "minimal")
    mcp = costs.estimate("aws", "medium", ["mcp"], "minimal")
    assert platform.lines[0].item == "VM (platform)" and platform.lines[0].usd == vm["small"]
    assert mcp.lines[0].item == "VM (mcp)" and mcp.lines[0].usd == vm["medium"]


def test_estimate_dedupes_components():
    costs = load_costs()
    for profile in ("standard", "minimal"):
        doubled = costs.estimate("aws", "small", ["platform", "platform"], profile)
        single = costs.estimate("aws", "small", ["platform"], profile)
        assert doubled == single, profile


def test_minimal_drops_second_vm_and_second_instance():
    costs = load_costs()
    standard = costs.estimate("aws", "small", ["platform", "mcp"], "standard")
    minimal = costs.estimate("aws", "small", ["platform", "mcp"], "minimal")
    assert {line.item for line in standard.lines} >= {
        "platform VM",
        "MCP VM",
        "state Postgres",
        "warehouse Postgres",
    }
    assert {line.item for line in minimal.lines} == {
        "VM (both components)",
        "Postgres (all databases)",
        "object storage and DNS",
    }


def test_snowflake_warehouse_has_no_warehouse_postgres_line():
    costs = load_costs()
    postgres = costs.estimate("aws", "small", ["platform", "mcp"], "standard")
    snowflake = costs.estimate("aws", "small", ["platform", "mcp"], "standard", "snowflake")
    assert "warehouse Postgres" not in {line.item for line in snowflake.lines}
    assert snowflake.monthly_usd < postgres.monthly_usd


def _postgres_line(estimate):
    return next(line for line in estimate.lines if line.item == "Postgres (all databases)")


def test_minimal_snowflake_prices_postgres_at_state_class():
    costs = load_costs()
    est = costs.estimate("aws", "medium", ["platform", "mcp"], "minimal", "snowflake")
    assert _postgres_line(est).usd == costs.table["aws"]["postgres"]["state"]


def test_minimal_postgres_uses_the_minimal_price_where_the_class_differs():
    costs = load_costs()
    scw = costs.table["scaleway"]
    small = costs.estimate("scaleway", "small", ["platform", "mcp"], "minimal")
    assert _postgres_line(small).usd == scw["postgres_minimal"]["small"]
    platform = costs.estimate("scaleway", "small", ["platform"], "minimal")
    assert _postgres_line(platform).usd == scw["postgres_minimal"]["small"]
    medium = costs.estimate("scaleway", "medium", ["platform", "mcp"], "minimal")
    assert _postgres_line(medium).usd == scw["postgres"]["medium"]
    snowflake = costs.estimate("scaleway", "small", ["platform", "mcp"], "minimal", "snowflake")
    assert _postgres_line(snowflake).usd == scw["postgres"]["state"]
    assert "postgres_minimal" not in costs.table["aws"]


def test_minimal_mcp_only_prices_postgres_at_state_class():
    costs = load_costs()
    est = costs.estimate("aws", "medium", ["mcp"], "minimal", "postgres")
    assert _postgres_line(est).usd == costs.table["aws"]["postgres"]["state"]


def test_guide_cloud_json_offline(invoke):
    r = invoke("guide", "cloud", "--json")
    assert r.exit_code == 0
    doc = json.loads(r.stdout)
    assert doc["topic"] == "cloud" and "aws" in doc["text"].lower()


def test_guide_default_is_entry_skill(invoke):
    r = invoke("guide", "--json")
    doc = json.loads(r.stdout)
    assert r.exit_code == 0 and doc["topic"] == "guide"
    assert doc["text"] == skill_body("guide")
    assert doc["text"].startswith("# ")


def test_guide_text_output_is_the_skill_body(invoke):
    r = invoke("guide")
    assert r.exit_code == 0
    assert r.stdout.rstrip("\n") == skill_body("guide").rstrip("\n")


def test_guide_unknown_topic(invoke):
    r = invoke("guide", "kubernetes", "--json")
    assert r.exit_code == 2


def test_estimate_json_carries_date_and_both_clouds(invoke):
    r = invoke("estimate", "--json")
    doc = json.loads(r.stdout)
    assert set(doc["clouds"]) == {"aws", "scaleway"}
    assert doc["gathered_on"].count("-") == 2
    assert doc["size"] == "small" and doc["profile"] == "standard"
    for cloud in doc["clouds"].values():
        assert cloud["minimal_monthly_usd"] < cloud["monthly_usd"]


def test_estimate_minimal_profile(invoke):
    r = invoke("estimate", "--profile", "minimal", "--json")
    doc = json.loads(r.stdout)
    assert doc["profile"] == "minimal"


@pytest.mark.parametrize(
    "args",
    [("--size", "xl"), ("--cloud", "gcp"), ("--cloud", ""), ("--components", "bi")],
)
def test_estimate_rejects_bad_values(invoke, args):
    assert invoke("estimate", *args, "--json").exit_code == 2
