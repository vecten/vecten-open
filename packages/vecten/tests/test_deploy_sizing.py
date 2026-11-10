import json
from pathlib import Path

import pytest

from vecten.deploy import sizing
from vecten.intake.sizing import THRESHOLDS
from vecten.knowhow import load_costs
from vecten_shared.output import ExitCode, Failure

REPO = Path(__file__).resolve().parents[3]
TIERS = ("small", "medium", "large")
FIELDS = {
    "vm",
    "warehouse_class",
    "minimal_class",
    "warehouse_storage_gb",
    "warehouse_max_storage_gb",
    "max_connections",
    "airflow_parallelism",
    "mcp_pool_cap",
    "users_max",
}
# Admin sessions and the setup host's SQL hold connections outside every pool.
RESERVED_CONNECTIONS = 20
CONCURRENT_PEOPLE_CAP = 20


@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
@pytest.mark.parametrize("size", TIERS)
def test_every_tier_has_every_field(cloud, size):
    entry = sizing.tier(cloud, size)
    assert set(entry) == FIELDS
    # AWS storage auto-grows up to the ceiling; Scaleway gets a fixed volume per tier.
    if cloud == "aws":
        assert entry["warehouse_max_storage_gb"] > entry["warehouse_storage_gb"]
    else:
        assert entry["warehouse_max_storage_gb"] == entry["warehouse_storage_gb"]


# The databases on the instance that holds the warehouse, per profile, on a Postgres project
# with the platform component.
WAREHOUSE_INSTANCE_DATABASES = {
    "standard": ("warehouse",),
    "minimal": ("airflow", "mcp_state", "warehouse"),
}
# Under minimal the same instance also serves Airflow's metadata pools and the MCP audit.
SHARED_STATE_CONNECTIONS = 60


CLOUD_SLICES = {"aws": sizing.aws_slice, "scaleway": sizing.scaleway_slice}


def class_used(cloud: str, size: str, profile: str) -> str:
    """The class the cloud module gives the instance holding the warehouse under `profile`.

    terraform/aws/locals.tf reads `warehouse_class`, which equals `minimal_class` on AWS;
    sizing.tftest.hcl pins that side.
    """
    key = "minimal_class" if profile == "minimal" else "warehouse_class"
    return sizing.tier(cloud, size)[key]


def connection_budgets(cloud: str) -> dict[str, int]:
    """Each class the table names, with its `max_connections`; one class, one figure."""
    budgets = {sizing.state_class(cloud): CLOUD_SLICES[cloud]()["state_max_connections"]}
    for size in TIERS:
        t = sizing.tier(cloud, size)
        known = budgets.setdefault(t["warehouse_class"], t["max_connections"])
        assert known == t["max_connections"], (cloud, t["warehouse_class"])
    return budgets


def max_connections_of(cloud: str, instance_class: str) -> int:
    budgets = connection_budgets(cloud)
    assert instance_class in budgets, f"{instance_class} has no connection budget in the table"
    return budgets[instance_class]


@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_minimal_class_is_the_warehouse_class_on_aws_only(cloud):
    same = [
        sizing.tier(cloud, s)["minimal_class"] == sizing.tier(cloud, s)["warehouse_class"]
        for s in TIERS
    ]
    assert all(same) if cloud == "aws" else same == [False, True, True]


@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_state_instance_budget(cloud):
    # Under standard the state instance holds Airflow's metadata pools and the MCP audit.
    limit = CLOUD_SLICES[cloud]()["state_max_connections"]
    assert SHARED_STATE_CONNECTIONS + RESERVED_CONNECTIONS < limit
    assert max_connections_of(cloud, sizing.state_class(cloud)) == limit


@pytest.mark.parametrize("size", TIERS)
@pytest.mark.parametrize("profile", sorted(WAREHOUSE_INSTANCE_DATABASES))
@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_connection_budget(cloud, size, profile):
    t = sizing.tier(cloud, size)
    databases = WAREHOUSE_INSTANCE_DATABASES[profile]
    people = min(t["users_max"], CONCURRENT_PEOPLE_CAP)
    budget = t["airflow_parallelism"] + t["mcp_pool_cap"] * people + RESERVED_CONNECTIONS
    if "airflow" in databases:
        budget += SHARED_STATE_CONNECTIONS
    limit = max_connections_of(cloud, class_used(cloud, size, profile))
    assert budget < limit, (cloud, size, profile)


VM_MEMORY_MB = {
    "t3.medium": 4096,
    "t3.large": 8192,
    "t3.xlarge": 16384,
    "DEV1-M": 4096,
    "DEV1-L": 8192,
    "POP2-2C-8G": 8192,
    "POP2-4C-16G": 16384,
}
# A LocalExecutor worker holds about 220 MB idle and more while a sync runs; the API server,
# scheduler, triggerer, dag processor, log server, nginx and the OS take the rest.
WORKER_MB, AIRFLOW_BASE_MB = 300, 2560


@pytest.mark.parametrize("size", TIERS)
@pytest.mark.parametrize("cloud", ["aws", "scaleway"])
def test_airflow_workers_fit_the_platform_vm(cloud, size):
    t = sizing.tier(cloud, size)
    used = t["airflow_parallelism"] * WORKER_MB + AIRFLOW_BASE_MB
    assert used <= VM_MEMORY_MB[t["vm"]], (cloud, size, used)


def test_aws_values():
    assert [sizing.tier("aws", s)["vm"] for s in TIERS] == ["t3.medium", "t3.large", "t3.xlarge"]
    assert [sizing.tier("aws", s)["warehouse_class"] for s in TIERS] == [
        "db.t4g.small",
        "db.m6g.large",
        "db.m6g.xlarge",
    ]
    assert [sizing.tier("aws", s)["warehouse_storage_gb"] for s in TIERS] == [50, 200, 500]
    assert [sizing.tier("aws", s)["warehouse_max_storage_gb"] for s in TIERS] == [200, 1000, 2000]
    assert sizing.state_class("aws") == "db.t4g.micro"
    assert sizing.aws_slice()["state_storage_gb"] == 20
    assert sizing.aws_slice()["state_max_connections"] == 100
    assert sizing.minimal_vm("aws") == "t3.large"


def test_scaleway_values():
    tiers = [sizing.tier("scaleway", s) for s in TIERS]
    assert [t["vm"] for t in tiers] == ["DEV1-M", "POP2-2C-8G", "POP2-4C-16G"]
    assert [t["warehouse_class"] for t in tiers] == ["DB-PLAY2-PICO", "DB-PRO2-XXS", "DB-PRO2-XS"]
    assert [t["warehouse_storage_gb"] for t in tiers] == [50, 200, 500]
    assert [t["max_connections"] for t in tiers] == [50, 200, 400]
    assert [t["minimal_class"] for t in tiers] == ["DB-PLAY2-NANO", "DB-PRO2-XXS", "DB-PRO2-XS"]
    assert sizing.minimal_vm("scaleway") == "DEV1-L"
    assert sizing.state_class("scaleway") == "DB-PLAY2-NANO"
    scw = sizing.scaleway_slice()
    assert scw["state_storage_gb"] == 20 and scw["volume_type"] == "sbs_5k"
    assert scw["state_max_connections"] == 100


def test_snowflake_warehouse_per_tier():
    assert [sizing.snowflake_warehouse_size(s) for s in TIERS] == ["XSMALL", "SMALL", "MEDIUM"]


def test_users_max_matches_the_intake_thresholds():
    for ceiling, size in THRESHOLDS:
        for cloud in ("aws", "scaleway"):
            assert sizing.tier(cloud, size)["users_max"] == ceiling


def test_unknown_cloud_or_size_refused():
    with pytest.raises(Failure) as exc:
        sizing.tier("gcp", "small")
    assert exc.value.code == ExitCode.CONFIG and "aws" in exc.value.message
    with pytest.raises(Failure) as exc:
        sizing.tier("aws", "huge")
    assert exc.value.code == ExitCode.CONFIG and "small" in exc.value.message
    with pytest.raises(Failure):
        sizing.snowflake_warehouse_size("huge")
    with pytest.raises(Failure):
        sizing.state_class("gcp")


def test_lookups_return_copies():
    sizing.tier("aws", "small")["vm"] = "x"
    sizing.aws_slice()["tiers"]["small"]["vm"] = "x"
    assert sizing.tier("aws", "small")["vm"] == "t3.medium"


def test_terraform_slice_is_the_package_aws_slice():
    expected = json.dumps(sizing.aws_slice(), indent=2, sort_keys=True) + "\n"
    assert (REPO / "terraform" / "aws" / "sizing.json").read_bytes() == expected.encode()


def test_terraform_slice_is_the_package_scaleway_slice():
    expected = json.dumps(sizing.scaleway_slice(), indent=2, sort_keys=True) + "\n"
    assert (REPO / "terraform" / "scaleway" / "sizing.json").read_bytes() == expected.encode()


def test_snowflake_module_sizing_is_the_package_warehouse_table():
    expected = json.dumps(sizing.snowflake_slice(), indent=2, sort_keys=True) + "\n"
    assert sizing.snowflake_slice() == {"small": "XSMALL", "medium": "SMALL", "large": "MEDIUM"}
    assert (REPO / "terraform" / "snowflake" / "sizing.json").read_bytes() == expected.encode()


def test_cost_table_tiers_are_the_sizing_tiers():
    costs = load_costs()
    assert tuple(costs.tiers) == TIERS == sizing.TIERS
    for cloud in costs.clouds:
        assert set(costs.table[cloud]["vm"]) - {"minimal"} == set(TIERS)
        assert set(costs.table[cloud]["postgres"]) - {"state"} == set(TIERS)
        assert set(costs.table[cloud].get("postgres_minimal", {})) <= set(TIERS)
