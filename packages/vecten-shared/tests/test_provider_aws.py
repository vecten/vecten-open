import importlib.util
import json
import sys
from pathlib import Path

import boto3
import boto3.session
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import (
    ConnectionSpec,
    LogTarget,
    get_provider,
)
from vecten_shared.provider.aws import RUNTIME_WRITABLE, AwsRuntimeProvider
from vecten_shared.provider.environment import (
    EnvironmentOutputs,
    HostOutput,
    PostgresOutput,
    environment_file,
    load_outputs,
)
from vecten_shared.provider.local import LocalProvider


def _load_fixtures():
    # Every package's test directory is a package named `tests`, so a relative import
    # resolves against whichever one pytest imported first; load the helper by path.
    name = "vecten_shared_tests_provider_fixtures"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("provider_fixtures.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


fixtures = _load_fixtures()
BUCKET = fixtures.BUCKET
REGION = fixtures.REGION
STANDARD_OUTPUTS = fixtures.STANDARD_OUTPUTS
STATE_HOST = fixtures.STATE_HOST
WAREHOUSE_HOST = fixtures.WAREHOUSE_HOST
aws_credentials = fixtures.aws_credentials
minimal_outputs = fixtures.minimal_outputs
write_aws_project = fixtures.write_aws_project
write_config = fixtures.write_config
write_outputs = fixtures.write_outputs


@pytest.fixture
def root(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_aws_project(tmp_path)
    return tmp_path


@pytest.fixture
def aws(root):
    with mock_aws():
        yield root


def secrets_client():
    return boto3.client("secretsmanager", region_name=REGION)


def no_cloud_api(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a cloud API client was created")

    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3, "resource", refuse)
    monkeypatch.setattr(boto3.session.Session, "client", refuse)
    monkeypatch.setattr(boto3.session.Session, "resource", refuse)


def test_outputs_load_into_typed_models(root):
    outputs = load_outputs(environment_file(root, "prod"), "prod")
    assert isinstance(outputs, EnvironmentOutputs)
    assert outputs.region == REGION and outputs.setup_host == "mcp"
    assert outputs.secret_prefix == "vecten/acme/prod/"
    assert outputs.hosts["platform"] == HostOutput(
        instance_id="i-0platform",
        public_ip="203.0.113.10",
        components=["platform"],
        hostnames=["airflow.data.acme.com"],
    )
    assert isinstance(outputs.postgres["state"], PostgresOutput)
    assert outputs.postgres["state"].port == 5432
    assert outputs.postgres["state"].databases == ["airflow", "mcp_state"]


def test_environment_file_honours_the_override(tmp_path, monkeypatch):
    monkeypatch.delenv("VECTEN_ENVIRONMENT_FILE", raising=False)
    assert environment_file(tmp_path, "prod") == tmp_path / ".vecten" / "environments" / "prod.json"
    elsewhere = tmp_path / "bundle" / "outputs.json"
    monkeypatch.setenv("VECTEN_ENVIRONMENT_FILE", str(elsewhere))
    assert environment_file(tmp_path, "prod") == elsewhere


def test_provider_reads_the_overridden_environment_file(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_config(tmp_path)
    elsewhere = tmp_path / "bundle" / "outputs.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text(json.dumps(STANDARD_OUTPUTS), encoding="utf-8")
    monkeypatch.setenv("VECTEN_ENVIRONMENT_FILE", str(elsewhere))
    spec = AwsRuntimeProvider(tmp_path, "prod").connection("prod", "warehouse")
    assert spec.host == WAREHOUSE_HOST


def test_warehouse_connection_comes_from_the_outputs_file_without_a_cloud_api(root, monkeypatch):
    no_cloud_api(monkeypatch)
    provider = AwsRuntimeProvider(root, "prod")
    spec = provider.connection("prod", "warehouse")
    assert spec == ConnectionSpec(
        kind="postgres",
        database="warehouse",
        role="loader",
        auth="password",
        host=WAREHOUSE_HOST,
        port=5432,
    )
    airflow = provider.connection("prod", "airflow")
    assert (airflow.host, airflow.database, airflow.role) == (STATE_HOST, "airflow", "airflow")
    mcp_state = provider.connection("prod", "mcp_state")
    assert (mcp_state.host, mcp_state.role) == (STATE_HOST, "mcp_state")


def test_minimal_outputs_put_every_database_on_one_host(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_aws_project(tmp_path, outputs=minimal_outputs())
    provider = AwsRuntimeProvider(tmp_path, "prod")
    specs = [provider.connection("prod", db) for db in ("warehouse", "airflow", "mcp_state")]
    assert {s.host for s in specs} == {STATE_HOST}
    assert [s.role for s in specs] == ["loader", "airflow", "mcp_state"]


def test_snowflake_warehouse_connection(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    outputs = json.loads(json.dumps(STANDARD_OUTPUTS))
    del outputs["postgres"]["warehouse"]
    write_aws_project(
        tmp_path, outputs=outputs, warehouse={"kind": "snowflake", "account": "ACMEORG-PROD1"}
    )
    no_cloud_api(monkeypatch)
    spec = AwsRuntimeProvider(tmp_path, "prod").connection("prod", "warehouse")
    assert spec == ConnectionSpec(
        kind="snowflake",
        database="ACME",
        role="ANALYST",
        auth="external_oauth",
        account="ACMEORG-PROD1",
        warehouse="ACME_PROD_WH",
    )


def mcp_only_outputs() -> dict:
    outputs = json.loads(json.dumps(STANDARD_OUTPUTS))
    del outputs["hosts"]["platform"]
    outputs["postgres"] = {
        "state": {**outputs["postgres"]["state"], "databases": ["mcp_state"]},
    }
    return outputs


def test_existing_postgres_warehouse_connects_as_the_impersonator(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_aws_project(
        tmp_path,
        outputs=mcp_only_outputs(),
        warehouse={
            "kind": "postgres",
            "existing": True,
            "host": "db.acme.internal",
            "port": 6432,
            "database": "analytics",
        },
    )
    no_cloud_api(monkeypatch)
    spec = AwsRuntimeProvider(tmp_path, "prod").connection("prod", "warehouse")
    assert spec == ConnectionSpec(
        kind="postgres",
        database="analytics",
        role="impersonator",
        auth="impersonation",
        host="db.acme.internal",
        port=6432,
    )


def test_existing_postgres_warehouse_without_a_database_is_a_config_failure(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_aws_project(
        tmp_path,
        outputs=mcp_only_outputs(),
        warehouse={"kind": "postgres", "existing": True, "host": "db.acme.internal"},
    )
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(tmp_path, "prod").connection("prod", "warehouse")
    assert exc.value.code == ExitCode.CONFIG
    assert "[warehouse] database" in exc.value.message


def test_existing_snowflake_warehouse_keeps_the_snowflake_spec(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_aws_project(
        tmp_path,
        outputs=mcp_only_outputs(),
        warehouse={
            "kind": "snowflake",
            "existing": True,
            "account": "ACMEORG-PROD1",
            "database": "analytics",
        },
    )
    spec = AwsRuntimeProvider(tmp_path, "prod").connection("prod", "warehouse")
    assert spec == ConnectionSpec(
        kind="snowflake",
        database="ANALYTICS",
        role="ANALYST",
        auth="external_oauth",
        account="ACMEORG-PROD1",
        warehouse="ACME_PROD_WH",
    )


def test_a_database_missing_from_the_outputs_names_deploy(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    outputs = json.loads(json.dumps(STANDARD_OUTPUTS))
    del outputs["postgres"]["warehouse"]
    write_aws_project(tmp_path, outputs=outputs)
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(tmp_path, "prod").connection("prod", "warehouse")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "warehouse" in exc.value.message and "vecten deploy --env prod" in exc.value.message


def test_connection_for_another_environment_is_refused(root):
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(root, "prod").connection("staging", "airflow")
    assert exc.value.code == ExitCode.USAGE
    assert "prod" in exc.value.message and "staging" in exc.value.message


def test_get_secret_reads_the_platform_scope(aws):
    secrets_client().create_secret(
        Name="vecten/acme/prod/platform/HUBSPOT_API_KEY", SecretString="hub-value"
    )
    assert AwsRuntimeProvider(aws, "prod").get_secret("HUBSPOT_API_KEY") == "hub-value"


def test_get_secret_reads_the_mcp_scope(aws):
    secrets_client().create_secret(
        Name="vecten/acme/prod/mcp/MCP_PROVISIONER_PASSWORD", SecretString="prov"
    )
    assert AwsRuntimeProvider(aws, "prod").get_secret("MCP_PROVISIONER_PASSWORD") == "prov"


def test_missing_secret_names_secret_set_for_the_environment(aws):
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(aws, "prod").get_secret("HUBSPOT_API_KEY")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "vecten secret set HUBSPOT_API_KEY --env prod" in exc.value.message


def test_binary_secret_names_secret_set(aws):
    secrets_client().create_secret(
        Name="vecten/acme/prod/platform/HUBSPOT_API_KEY", SecretBinary=b"\x00\x01"
    )
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(aws, "prod").get_secret("HUBSPOT_API_KEY")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "no text value" in exc.value.message
    assert "vecten secret set HUBSPOT_API_KEY --env prod" in exc.value.message


def test_runtime_writes_only_the_impersonator_password(aws):
    assert RUNTIME_WRITABLE == frozenset({"MCP_IMPERSONATOR_PASSWORD", "MCP_SIGNING_KEY"})
    provider = AwsRuntimeProvider(aws, "prod")
    with pytest.raises(Failure) as exc:
        provider.put_secret("HUBSPOT_API_KEY", "nope-value")
    assert exc.value.code == ExitCode.REFUSED
    assert "vecten CLI" in exc.value.message and "nope-value" not in exc.value.message
    assert provider.list_secrets() == []


def test_impersonator_password_lands_in_the_mcp_scope(aws):
    provider = AwsRuntimeProvider(aws, "prod")
    provider.put_secret("MCP_IMPERSONATOR_PASSWORD", "v1")
    provider.put_secret("MCP_IMPERSONATOR_PASSWORD", "v2")
    stored = secrets_client().get_secret_value(
        SecretId="vecten/acme/prod/mcp/MCP_IMPERSONATOR_PASSWORD"
    )
    assert stored["SecretString"] == "v2"


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("bootstrap", ("prod", "acme/data")),
        ("provision", ("prod",)),
        ("destroy", ("prod",)),
        ("ship", ("prod", "platform", "/tmp/project")),
    ],
)
def test_control_plane_operations_are_refused_at_runtime(root, operation, args):
    with pytest.raises(Failure) as exc:
        getattr(AwsRuntimeProvider(root, "prod"), operation)(*args)
    assert exc.value.code == ExitCode.REFUSED
    assert operation in exc.value.message and "vecten CLI" in exc.value.message


def test_log_target_is_the_platform_logs_prefix(root, monkeypatch):
    no_cloud_api(monkeypatch)
    assert AwsRuntimeProvider(root, "prod").log_target("prod") == LogTarget(
        "bucket", f"s3://{BUCKET}/platform/logs"
    )


def test_missing_outputs_file_names_deploy(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_config(tmp_path)
    provider = AwsRuntimeProvider(tmp_path, "prod")
    for call in (
        lambda: provider.connection("prod", "airflow"),
        lambda: provider.get_secret("HUBSPOT_API_KEY"),
        lambda: provider.log_target("prod"),
    ):
        with pytest.raises(Failure) as exc:
            call()
        assert exc.value.code == ExitCode.PREREQUISITE
        assert "vecten deploy --env prod" in exc.value.message


@pytest.mark.parametrize(
    "content", ["{not json", json.dumps({"environment": "prod"}), json.dumps([1])]
)
def test_malformed_outputs_file_is_a_config_failure(tmp_path, monkeypatch, content):
    aws_credentials(monkeypatch, tmp_path)
    write_config(tmp_path)
    path = write_outputs(tmp_path, {})
    path.write_text(content, encoding="utf-8")
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(tmp_path, "prod").connection("prod", "airflow")
    assert exc.value.code == ExitCode.CONFIG
    assert str(path) in exc.value.message


class Raising:
    def __init__(self, error):
        self.error = error

    def __getattr__(self, operation):
        def call(**kwargs):
            raise self.error

        return call


def leaky_error(operation):
    return ClientError(
        {"Error": {"Code": "InternalServiceError", "Message": "the value is s3cr3t"}},
        operation,
    )


@pytest.mark.parametrize(
    ("call", "operation"),
    [
        (lambda p: p.get_secret("HUBSPOT_API_KEY"), "GetSecretValue"),
        (lambda p: p.put_secret("MCP_IMPERSONATOR_PASSWORD", "s3cr3t"), "PutSecretValue"),
        (lambda p: p.list_secrets(), "ListSecrets"),
    ],
)
def test_client_error_message_names_type_and_code_only(root, monkeypatch, call, operation):
    monkeypatch.setattr(boto3, "client", lambda *a, **k: Raising(leaky_error(operation)))
    with pytest.raises(Failure) as exc:
        call(AwsRuntimeProvider(root, "prod"))
    assert exc.value.code == ExitCode.TOOL_FAILED
    assert "s3cr3t" not in exc.value.message
    assert "the value is" not in exc.value.message
    assert exc.value.message == (
        f"Secrets Manager {operation} failed: ClientError InternalServiceError"
    )
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "Bad Code: s3cr3t", "Message": "s3cr3t"}}, "ListSecrets"),
        ClientError({"Error": {"Message": "s3cr3t"}}, "ListSecrets"),
        NoCredentialsError(),
    ],
    ids=["code-with-spaces", "no-code", "botocore-error"],
)
def test_an_error_without_a_plain_code_names_its_type_only(root, monkeypatch, error):
    monkeypatch.setattr(boto3, "client", lambda *a, **k: Raising(error))
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(root, "prod").list_secrets()
    assert exc.value.message == f"Secrets Manager ListSecrets failed: {type(error).__name__}"


def test_list_secrets_lists_names_under_the_environment_prefix(aws):
    client = secrets_client()
    for name in (
        "vecten/acme/prod/platform/STRIPE_KEY",
        "vecten/acme/prod/mcp/MCP_STATE_DB_PASSWORD",
        "vecten/acme/prod/platform/HUBSPOT_API_KEY",
        "vecten/acme/staging/platform/OTHER_ENV_KEY",
        "vecten/acmecorp/prod/platform/OTHER_PROJECT_KEY",
        "unrelated/secret",
    ):
        client.create_secret(Name=name, SecretString="x")
    assert AwsRuntimeProvider(aws, "prod").list_secrets() == [
        "HUBSPOT_API_KEY",
        "MCP_STATE_DB_PASSWORD",
        "STRIPE_KEY",
    ]


def test_get_provider_binds_the_environment(root):
    provider = get_provider("aws", root, "prod")
    assert isinstance(provider, AwsRuntimeProvider) and provider.env == "prod"
    assert isinstance(get_provider("local", root, "prod"), LocalProvider)
    assert isinstance(get_provider("local", root), LocalProvider)


def test_missing_override_file_names_the_bound_environment(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_config(tmp_path)
    elsewhere = tmp_path / "bundle" / "outputs.json"
    monkeypatch.setenv("VECTEN_ENVIRONMENT_FILE", str(elsewhere))
    with pytest.raises(Failure) as exc:
        load_outputs(elsewhere, "prod")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "vecten deploy --env prod" in exc.value.message
    assert "--env outputs" not in exc.value.message
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(tmp_path, "prod").get_secret("HUBSPOT_API_KEY")
    assert "vecten deploy --env prod" in exc.value.message
    assert str(elsewhere) in exc.value.message


def test_outputs_of_another_environment_are_refused(tmp_path, monkeypatch):
    aws_credentials(monkeypatch, tmp_path)
    write_config(tmp_path)
    write_outputs(tmp_path, {**STANDARD_OUTPUTS, "environment": "staging"})
    with pytest.raises(Failure) as exc:
        AwsRuntimeProvider(tmp_path, "prod").connection("prod", "airflow")
    assert exc.value.code == ExitCode.CONFIG
    assert "staging" in exc.value.message and "prod" in exc.value.message


def test_constructing_the_provider_reads_nothing(tmp_path, monkeypatch):
    no_cloud_api(monkeypatch)
    provider = AwsRuntimeProvider(tmp_path / "absent", "prod")
    assert provider.env == "prod"
