"""The control-plane AWS provider keeps the runtime contract and lifts its write restriction."""

import json

import boto3
import pytest
from moto import mock_aws

from vecten.deploy import aws as deploy_aws
from vecten.deploy.aws import AwsProvider, clients
from vecten_shared import provider as provider_module
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import LogTarget, get_provider
from vecten_shared.provider.aws import AwsRuntimeProvider

from .deploy_fixtures import REGION, STANDARD_OUTPUTS, aws_credentials, render_aws, write_outputs

OPERATIONS = (
    "bootstrap",
    "provision",
    "destroy",
    "put_secret",
    "get_secret",
    "connection",
    "log_target",
    "ship",
    "list_secrets",
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    root = render_aws(tmp_path / "proj")
    write_outputs(root, STANDARD_OUTPUTS)
    return root


@pytest.fixture
def aws(root):
    with mock_aws():
        yield root


def stored(name):
    client = boto3.client("secretsmanager", region_name=REGION)
    return client.get_secret_value(SecretId=name)["SecretString"]


def test_every_operation_is_offered(root):
    provider = AwsProvider(root, "prod")
    assert isinstance(provider, AwsRuntimeProvider)
    for operation in OPERATIONS:
        assert callable(getattr(provider, operation, None)), operation


@pytest.mark.parametrize("db", ["warehouse", "airflow", "mcp_state"])
def test_connections_match_the_runtime_class(root, db):
    assert AwsProvider(root, "prod").connection("prod", db) == AwsRuntimeProvider(
        root, "prod"
    ).connection("prod", db)


def test_log_target_matches_the_runtime_class(root):
    target = AwsProvider(root, "prod").log_target("prod")
    assert isinstance(target, LogTarget)
    assert target == AwsRuntimeProvider(root, "prod").log_target("prod")


def test_unknown_database_names_the_three_values(root):
    with pytest.raises(Failure) as exc:
        AwsProvider(root, "prod").connection("prod", "nosuch")
    assert exc.value.code == ExitCode.USAGE
    assert "warehouse, airflow, mcp_state" in exc.value.message


@pytest.mark.parametrize("name", ["HUBSPOT_API_KEY", "LOADER_PASSWORD", "AIRFLOW_DB_PASSWORD"])
def test_put_secret_writes_any_platform_name(aws, name):
    provider = AwsProvider(aws, "prod")
    provider.put_secret(name, "first-value")
    provider.put_secret(name, "second-value")
    assert stored(f"vecten/acme/prod/platform/{name}") == "second-value"
    assert provider.get_secret(name) == "second-value"
    assert name in provider.list_secrets()


def test_put_secret_places_other_scopes_under_their_prefix(aws):
    provider = AwsProvider(aws, "prod")
    provider.put_secret("DEPLOY_SNOWFLAKE_USER", "deployer")
    provider.put_secret("MCP_PROVISIONER_PASSWORD", "p")
    assert stored("vecten/acme/prod/deploy/DEPLOY_SNOWFLAKE_USER") == "deployer"
    assert stored("vecten/acme/prod/mcp/MCP_PROVISIONER_PASSWORD") == "p"


def test_clients_come_from_the_factory(aws, monkeypatch):
    seen = []
    real = clients.client

    def client(service, region):
        seen.append((service, region))
        return real(service, region)

    monkeypatch.setattr(clients, "client", client)
    AwsProvider(aws, "prod").put_secret("HUBSPOT_API_KEY", "v")
    assert seen == [("secretsmanager", REGION)]


def test_region_comes_from_tfvars_before_the_first_deploy(aws):
    (aws / ".vecten" / "environments" / "prod.json").unlink()
    provider = AwsProvider(aws, "prod")
    assert provider.region() == "eu-central-1"
    provider.put_secret("HUBSPOT_API_KEY", "early")
    assert stored("vecten/acme/prod/platform/HUBSPOT_API_KEY") == "early"


def test_importing_the_control_plane_registers_it(root, monkeypatch):
    monkeypatch.delitem(provider_module._REGISTRY, "aws", raising=False)
    deploy_aws.register()
    assert isinstance(get_provider("aws", root, "prod"), AwsProvider)


def test_registration_replaces_the_runtime_class(root, monkeypatch):
    monkeypatch.setitem(provider_module._REGISTRY, "aws", AwsRuntimeProvider)
    deploy_aws.register()
    assert type(get_provider("aws", root, "prod")) is AwsProvider


def test_registration_keeps_another_factory(root, monkeypatch):
    class Other(AwsRuntimeProvider):
        pass

    monkeypatch.setitem(provider_module._REGISTRY, "aws", Other)
    deploy_aws.register()
    assert type(get_provider("aws", root, "prod")) is Other


@pytest.fixture
def cli_project(invoke, aws, monkeypatch):
    monkeypatch.chdir(aws)
    # The runtime class in the slot is what a fresh process holds before the CLI imports
    # the control plane.
    monkeypatch.setitem(provider_module._REGISTRY, "aws", AwsRuntimeProvider)
    (aws / ".vecten" / "environments" / "prod.json").unlink()
    return aws


def test_secret_set_on_aws_goes_through_the_full_provider(invoke, cli_project):
    r = invoke("secret", "set", "HUBSPOT_API_KEY", "--env", "prod", "--json", input="cloudy\n")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["env"] == "prod"
    assert stored("vecten/acme/prod/platform/HUBSPOT_API_KEY") == "cloudy"
    assert "cloudy" not in r.stdout + r.stderr


def test_secret_list_on_aws_names_what_was_set(invoke, cli_project):
    invoke("secret", "set", "HUBSPOT_API_KEY", "--env", "prod", input="v\n")
    r = invoke("secret", "list", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["names"] == ["HUBSPOT_API_KEY"]
