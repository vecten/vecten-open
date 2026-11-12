import json

import pytest

from vecten.deploy.outputs import OUTPUT_NAMES, outputs_key, outputs_path, write_outputs
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.environment import (
    ENVIRONMENT_FILE_VAR,
    EnvironmentOutputs,
    load_outputs,
)

VALUES = {
    "environment": "prod",
    "region": "eu-central-1",
    "hosts": {
        "mcp": {
            "instance_id": "i-0abc",
            "public_ip": "203.0.113.7",
            "components": ["mcp"],
            "hostnames": ["mcp.data.acme.com"],
        }
    },
    "setup_host": "mcp",
    "bucket": "vecten-acme-prod-data",
    "secret_prefix": "vecten/acme/prod/",
    "postgres": {
        "state": {
            "host": "state.example.rds.amazonaws.com",
            "port": 5432,
            "databases": ["mcp_state"],
            "master_secret": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:x",
        }
    },
}


def terraform_json(values):
    return {k: {"sensitive": False, "type": "ignored", "value": v} for k, v in values.items()}


def test_output_names_are_the_environment_outputs_fields():
    assert OUTPUT_NAMES == (
        "environment",
        "region",
        "hosts",
        "setup_host",
        "bucket",
        "secret_prefix",
        "postgres",
    )
    assert set(OUTPUT_NAMES) == set(EnvironmentOutputs.model_fields)


def test_paths(tmp_path, monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_FILE_VAR, str(tmp_path / "elsewhere.json"))
    assert outputs_path(tmp_path, "prod") == tmp_path / ".vecten" / "environments" / "prod.json"
    assert outputs_key("prod") == "prod/outputs.json"


def test_write_outputs_keeps_values_and_round_trips(tmp_path):
    written = write_outputs(tmp_path, "prod", terraform_json(VALUES))
    assert isinstance(written, EnvironmentOutputs)
    path = outputs_path(tmp_path, "prod")
    assert json.loads(path.read_text()) == VALUES
    assert load_outputs(path, "prod") == written


def test_extra_outputs_are_dropped(tmp_path):
    raw = terraform_json({**VALUES, "debug": "x"})
    write_outputs(tmp_path, "prod", raw)
    assert "debug" not in json.loads(outputs_path(tmp_path, "prod").read_text())


def test_missing_output_refused_and_nothing_written(tmp_path):
    raw = terraform_json({k: v for k, v in VALUES.items() if k != "bucket"})
    with pytest.raises(Failure) as exc:
        write_outputs(tmp_path, "prod", raw)
    assert exc.value.code == ExitCode.TOOL_FAILED and "bucket" in exc.value.message
    assert not outputs_path(tmp_path, "prod").exists()


def test_malformed_output_refused(tmp_path):
    raw = terraform_json({**VALUES, "postgres": {"state": {"host": "h"}}})
    with pytest.raises(Failure) as exc:
        write_outputs(tmp_path, "prod", raw)
    assert exc.value.code == ExitCode.TOOL_FAILED and "postgres" in exc.value.message


def test_outputs_of_another_environment_refused(tmp_path):
    with pytest.raises(Failure) as exc:
        write_outputs(tmp_path, "staging", terraform_json(VALUES))
    assert "'prod'" in exc.value.message and "'staging'" in exc.value.message
    assert not outputs_path(tmp_path, "staging").exists()


def test_a_host_without_service_hostnames_reads_as_empty_and_writes_as_before():
    from vecten.deploy.outputs import outputs_text

    outputs = EnvironmentOutputs.model_validate(VALUES)
    assert outputs.hosts["mcp"].service_hostnames == []
    # Outputs from a module without the field keep their bytes in the bundle.
    assert json.loads(outputs_text(outputs)) == VALUES


def test_a_module_listing_no_service_hostname_writes_the_same_file():
    from vecten.deploy.outputs import outputs_text

    listed = json.loads(json.dumps(VALUES))
    listed["hosts"]["mcp"]["service_hostnames"] = []
    assert outputs_text(EnvironmentOutputs.model_validate(listed)) == outputs_text(
        EnvironmentOutputs.model_validate(VALUES)
    )


def test_service_hostnames_are_kept_per_host():
    from vecten.deploy.outputs import outputs_text

    listed = json.loads(json.dumps(VALUES))
    listed["hosts"]["mcp"]["service_hostnames"] = ["portal.data.acme.com"]
    outputs = EnvironmentOutputs.model_validate(listed)
    assert outputs.hosts["mcp"].service_hostnames == ["portal.data.acme.com"]
    assert json.loads(outputs_text(outputs)) == listed
