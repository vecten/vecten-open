"""Bound services in the generated Compose file: one per binding, isolated from the stack."""

import pytest

from vecten.stack.compose import SERVICES_BRIDGE, SERVICES_NETWORK, generate_compose
from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import Extension, record_text
from vecten_shared.services import parse_service
from vecten_shared.versions import VERSIONS

DEPLOY = {
    "cloud": "scaleway",
    "region": "fr-par",
    "domain": "data.acme.example",
    "dns_zone": "acme.example",
    "github_repository": "acme/platform",
}
VM_HOSTS = {"airflow": "airflow.data.acme.example", "mcp": "mcp.data.acme.example"}
BOT = {
    "command": ["python", "-m", "acme.bot", "--token", "$HOME"],
    "port": 8000,
    "health": {"path": "/healthz"},
    "routes": {"webhook": {"path": "/api/messages", "access": "public"}},
}
SECRETS = ("ACME_API_KEY", "MCP_ACME_TOKEN")


def config(services=None, **extra):
    return VectenConfig.model_validate(
        {
            "users": 5,
            "project": {"name": "acme", "scaffold_version": "0.1.0"},
            "extensions": ["acme"],
            "deploy": DEPLOY,
            "services": {"acme": services} if services else {},
            **extra,
        }
    )


def stack(tmp_path, declared=None, secrets=SECRETS):
    root = tmp_path / "proj"
    stack_dir = root / ".vecten" / "stack"
    stack_dir.mkdir(parents=True)
    services = {
        name: parse_service("acme", name, value).service
        for name, value in (declared if declared is not None else {"bot": BOT}).items()
    }
    record = {"acme": Extension("1.0", (), secrets=secrets, services=services)}
    (root / "extensions.lock.json").write_text(record_text(record))
    return stack_dir


BOUND = {"bot": {"hostname": "bot", "routes": {"webhook": {"access": "public"}}}}


def test_a_project_without_bindings_has_no_service_and_no_services_network(tmp_path):
    for mode, hosts in (("local", None), ("vm", VM_HOSTS)):
        doc = generate_compose(
            config(), mode, ["platform", "mcp"], stack(tmp_path / mode), hosts=hosts
        )
        assert not [name for name in doc["services"] if name.startswith("svc-")]
        assert "networks" not in doc
        assert "networks" not in doc["services"]["nginx"]


def test_a_bound_service_runs_from_its_host_components_project_image(tmp_path):
    doc = generate_compose(config(BOUND), "local", ["platform", "mcp"], stack(tmp_path))
    svc = doc["services"]["svc-acme-bot"]
    assert svc["image"] == f"{doc['name']}-airflow:{VERSIONS.vecten}"
    assert svc["pull_policy"] == "never"
    assert "build" not in svc
    assert svc["profiles"] == ["platform"]
    assert svc["command"] == ["python", "-m", "acme.bot", "--token", "$$HOME"]
    assert svc["restart"] == "unless-stopped"
    assert (svc["mem_limit"], svc["cpus"]) == ("256m", 0.5)
    assert svc["networks"] == [SERVICES_NETWORK]
    assert "ports" not in svc and "depends_on" not in svc
    assert svc["environment"] == {
        "VECTEN_ENV": "local",
        "VECTEN_COMPONENT": "service",
        "VECTEN_SERVICE": "acme.bot",
        "VECTEN_PROJECT_ROOT": "/opt/airflow",
        "PORT": "8000",
        "VECTEN_PUBLIC_URL": "https://bot.localhost:8443",
        "ACME_API_KEY": "${ACME_API_KEY:-}",
    }
    assert "../../vecten.toml:/opt/airflow/vecten.toml:ro" in svc["volumes"]
    assert "../../extensions.lock.json:/opt/airflow/extensions.lock.json:ro" in svc["volumes"]
    assert svc["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "--fail",
        "--silent",
        "--output",
        "/dev/null",
        "http://localhost:8000/healthz",
    ]


def test_nothing_waits_for_a_service_and_only_nginx_joins_its_network(tmp_path):
    doc = generate_compose(config(BOUND), "local", ["platform", "mcp"], stack(tmp_path))
    for name, svc in doc["services"].items():
        assert "svc-acme-bot" not in svc.get("depends_on", {}), name
        if name not in ("nginx", "svc-acme-bot"):
            assert "networks" not in svc, name
    assert doc["services"]["nginx"]["networks"] == ["default", SERVICES_NETWORK]
    # Two laptop projects would collide on one fixed bridge name; only a VM pins it.
    assert doc["networks"] == {SERVICES_NETWORK: {}}


def test_on_a_vm_the_bridge_name_is_fixed_and_no_cloud_key_reaches_the_service(tmp_path):
    doc = generate_compose(
        config(BOUND), "vm", ["platform", "mcp"], stack(tmp_path), hosts=VM_HOSTS
    )
    assert doc["networks"] == {
        SERVICES_NETWORK: {"driver_opts": {"com.docker.network.bridge.name": SERVICES_BRIDGE}}
    }
    assert len(SERVICES_BRIDGE) <= 15
    env = doc["services"]["svc-acme-bot"]["environment"]
    assert env["VECTEN_ENV"] == "${VECTEN_ENV}"
    assert env["VECTEN_PUBLIC_URL"] == "https://bot.data.acme.example"
    for name in env:
        assert not name.startswith(("SCW_", "AWS_", "AIRFLOW_CONN_")), name
    assert "VECTEN_PROVIDER" not in env
    assert "ports" not in doc["services"]["svc-acme-bot"]


def test_the_apex_public_url_is_the_domain(tmp_path):
    services = {"bot": {**BOUND["bot"], "hostname": "@"}}
    doc = generate_compose(config(services), "vm", ["platform"], stack(tmp_path), hosts=VM_HOSTS)
    assert doc["services"]["svc-acme-bot"]["environment"]["VECTEN_PUBLIC_URL"] == (
        "https://data.acme.example"
    )


def test_a_service_without_a_hostname_has_no_public_url(tmp_path):
    worker = {"command": ["run"], "health": {"command": ["check", "$X"]}}
    doc = generate_compose(
        config({"worker": {}}), "local", ["platform", "mcp"], stack(tmp_path, {"worker": worker})
    )
    svc = doc["services"]["svc-acme-worker"]
    assert "VECTEN_PUBLIC_URL" not in svc["environment"] and "PORT" not in svc["environment"]
    assert svc["healthcheck"]["test"] == ["CMD", "check", "$$X"]


def test_on_the_mcp_host_the_service_runs_from_the_mcp_image_with_mcp_secrets(tmp_path):
    services = {"bot": {**BOUND["bot"], "host": "mcp"}}
    declared = {"bot": {**BOT, "components": ["platform", "mcp"]}}
    doc = generate_compose(
        config(services), "local", ["platform", "mcp"], stack(tmp_path, declared)
    )
    svc = doc["services"]["svc-acme-bot"]
    assert svc["image"] == f"{doc['name']}-mcp:{VERSIONS.vecten}"
    assert svc["profiles"] == ["mcp"]
    assert svc["environment"]["VECTEN_PROJECT_ROOT"] == "/app"
    assert svc["environment"]["MCP_ACME_TOKEN"] == "${MCP_ACME_TOKEN:-}"
    assert "ACME_API_KEY" not in svc["environment"]


def test_limits_come_from_the_binding_over_the_declaration(tmp_path):
    services = {"bot": {**BOUND["bot"], "memory_mb": 512, "cpus": 1.25}}
    doc = generate_compose(config(services), "local", ["platform", "mcp"], stack(tmp_path))
    svc = doc["services"]["svc-acme-bot"]
    assert (svc["mem_limit"], svc["cpus"]) == ("512m", 1.25)


def test_a_declared_volume_is_a_named_volume_kept_by_the_project(tmp_path):
    declared = {"bot": {**BOT, "volume": "/var/lib/acme-bot"}}
    doc = generate_compose(config(BOUND), "local", ["platform", "mcp"], stack(tmp_path, declared))
    assert "svc-acme-bot-data:/var/lib/acme-bot" in doc["services"]["svc-acme-bot"]["volumes"]
    assert doc["volumes"]["svc-acme-bot-data"] == {}


def test_a_service_off_the_laptop_runs_only_deployed(tmp_path):
    services = {"bot": {**BOUND["bot"], "local": False}}
    local = generate_compose(config(services), "local", ["platform", "mcp"], stack(tmp_path / "a"))
    assert "svc-acme-bot" not in local["services"] and "networks" not in local
    vm = generate_compose(
        config(services), "vm", ["platform"], stack(tmp_path / "b"), hosts=VM_HOSTS
    )
    assert "svc-acme-bot" in vm["services"]


@pytest.mark.parametrize(
    ("components", "host_components", "host", "runs"),
    [
        # standard: each VM renders its own component
        (["platform", "mcp"], ["platform"], None, True),
        (["platform", "mcp"], ["mcp"], None, False),
        (["platform", "mcp"], ["mcp"], "mcp", True),
        (["platform", "mcp"], ["platform"], "mcp", False),
        # minimal: one VM renders both
        (["platform", "mcp"], ["platform", "mcp"], "mcp", True),
        # MCP-only project
        (["mcp"], ["mcp"], "mcp", True),
    ],
)
def test_a_service_runs_only_on_the_host_its_binding_names(
    tmp_path, components, host_components, host, runs
):
    binding = {**BOUND["bot"], **({"host": host} if host else {})}
    declared = {"bot": {**BOT, "components": ["platform", "mcp"]}}
    doc = generate_compose(
        config({"bot": binding}, components=components),
        "vm",
        host_components,
        stack(tmp_path, declared),
        hosts=VM_HOSTS,
    )
    assert ("svc-acme-bot" in doc["services"]) is runs
    assert ("networks" in doc) is runs


def test_the_host_image_creates_each_declared_volume_for_its_runtime_user(tmp_path):
    from vecten.stack.images import volume_step

    declared = {"bot": {**BOT, "volume": "/var/lib/acme-bot"}}
    root = stack(tmp_path, declared).parent.parent
    assert volume_step(config(BOUND), root, "airflow") == (
        "USER root\nRUN mkdir -p /var/lib/acme-bot && chown airflow /var/lib/acme-bot\n"
        "USER airflow\n"
    )
    assert volume_step(config(BOUND), root, "mcp") == ""
    assert volume_step(config(), root, "airflow") == ""
