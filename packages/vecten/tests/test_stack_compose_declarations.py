"""Declared secrets reach the component that reads them; the record and component reach both."""

import pytest

from vecten.stack.compose import generate_compose, platform_secrets
from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import Extension, record_text
from vecten_shared.output import Failure

DEPLOY = {
    "cloud": "aws",
    "region": "eu-central-1",
    "domain": "data.acme.example",
    "dns_zone": "acme.example",
    "github_repository": "acme/platform",
}
VM_HOSTS = {"airflow": "airflow.data.acme.example", "mcp": "mcp.data.acme.example"}
RECORD = {"acme": Extension("1.0", (), secrets=("ACME_API_KEY", "MCP_ACME_TOKEN"))}


def config(**extra):
    return VectenConfig.model_validate(
        {
            "users": 5,
            "project": {"name": "acme", "scaffold_version": "0.1.0"},
            "extensions": ["acme"],
            "secrets": ["SLACK_TOKEN", "MCP_CRM_TOKEN"],
            "deploy": DEPLOY,
            **extra,
        }
    )


def stack(tmp_path, record=RECORD):
    root = tmp_path / "proj"
    stack_dir = root / ".vecten" / "stack"
    stack_dir.mkdir(parents=True)
    if record is not None:
        (root / "extensions.lock.json").write_text(record_text(record))
    return stack_dir


def env(doc, service):
    return doc["services"][service]["environment"]


def test_platform_names_reach_airflow_and_mcp_names_the_laptops_mcp_server(tmp_path):
    doc = generate_compose(config(), "local", ["platform", "mcp"], stack(tmp_path))
    airflow = env(doc, "airflow-scheduler")
    assert airflow["ACME_API_KEY"] == "${ACME_API_KEY:-}"
    assert airflow["SLACK_TOKEN"] == "${SLACK_TOKEN:-}"
    assert "MCP_ACME_TOKEN" not in airflow
    mcp = env(doc, "mcp-server")
    assert mcp["MCP_ACME_TOKEN"] == "${MCP_ACME_TOKEN:-}"
    assert mcp["MCP_CRM_TOKEN"] == "${MCP_CRM_TOKEN:-}"
    assert "ACME_API_KEY" not in mcp


def test_on_a_vm_the_mcp_server_reads_its_secrets_through_its_provider(tmp_path):
    doc = generate_compose(
        config(), "vm", ["platform", "mcp"], stack(tmp_path), project="acme", hosts=VM_HOSTS
    )
    assert env(doc, "airflow-scheduler")["ACME_API_KEY"] == "${ACME_API_KEY:-}"
    assert "MCP_ACME_TOKEN" not in env(doc, "mcp-server")


def test_the_record_is_mounted_beside_vecten_toml_and_the_component_is_named(tmp_path):
    doc = generate_compose(config(), "local", ["platform", "mcp"], stack(tmp_path))
    airflow = doc["services"]["airflow-scheduler"]
    assert "../../extensions.lock.json:/opt/airflow/extensions.lock.json:ro" in airflow["volumes"]
    assert airflow["environment"]["VECTEN_COMPONENT"] == "platform"
    mcp = doc["services"]["mcp-server"]
    assert "../../extensions.lock.json:/app/extensions.lock.json:ro" in mcp["volumes"]
    assert mcp["environment"]["VECTEN_COMPONENT"] == "mcp"
    assert mcp["environment"]["VECTEN_PROJECT_ROOT"] == "/app"


def test_without_a_record_nothing_is_mounted(tmp_path):
    doc = generate_compose(
        config(extensions=[], secrets=[]), "local", ["platform"], stack(tmp_path, None)
    )
    assert not any(
        "extensions.lock.json" in v for v in doc["services"]["airflow-scheduler"]["volumes"]
    )


def test_a_declared_name_colliding_with_a_stack_variable_is_refused(tmp_path):
    with pytest.raises(Failure, match="FORWARDED_ALLOW_IPS"):
        generate_compose(
            config(secrets=["FORWARDED_ALLOW_IPS"]), "local", ["platform"], stack(tmp_path)
        )


def test_platform_secrets_unite_connectors_the_project_and_extensions():
    c = config(connectors=[{"name": "crm", "source": "rest", "secrets": ["CRM_TOKEN"]}])
    assert platform_secrets(c, RECORD) == ["ACME_API_KEY", "CRM_TOKEN", "SLACK_TOKEN"]


def test_on_a_vm_a_connector_secret_cannot_shadow_the_hosts_own_variables(tmp_path):
    c = config(
        extensions=[],
        secrets=[],
        connectors=[{"name": "crm", "source": "rest", "secrets": ["VECTEN_BUNDLE_LINEAGE"]}],
    )
    with pytest.raises(Failure, match="VECTEN_BUNDLE_LINEAGE"):
        generate_compose(
            c, "vm", ["platform"], stack(tmp_path, None), project="acme", hosts=VM_HOSTS
        )
