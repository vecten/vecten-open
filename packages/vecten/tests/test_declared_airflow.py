import json

import pytest

from vecten.stack.airflow_auth import passwords
from vecten.stack.compose import generate_compose
from vecten_shared.airflow_resources import effective_resources
from vecten_shared.config import VectenConfig
from vecten_shared.declarations import parse_manifest
from vecten_shared.extension_record import Extension, parse_record, record_text
from vecten_shared.output import Failure


def test_declared_resources_reach_both_stack_modes_and_the_local_seed(tmp_path):
    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "demo", "scaffold_version": "0.1.1"},
            "airflow": {"pools": {"purchases": 2}},
        }
    )
    manifest = parse_manifest(
        "alerts",
        {
            "secrets": ["MONITOR_PASSWORD"],
            "airflow": {
                "api_accounts": {"monitor": "MONITOR_PASSWORD"},
                "variables": {"pause": "no"},
            },
        },
    )
    record = {"alerts": Extension("1", (), secrets=manifest.secrets, airflow=manifest.airflow)}
    contents = record_text(record)
    assert parse_record(contents) == record
    (tmp_path / "extensions.lock.json").write_text(contents)
    stack = tmp_path / ".vecten/stack"
    for mode, hosts in [
        ("local", None),
        ("vm", {"airflow": "airflow.demo.example", "mcp": "mcp.demo.example"}),
    ]:
        doc = generate_compose(config, mode, ["platform"], stack, hosts=hosts)
        services = doc["services"]
        api = services["airflow-api-server"]
        assert "vecten_platform.airflow_resources" in api["command"][-1]
        assert api["environment"][
            "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS"
        ] == "${AIRFLOW_ADMIN_USER}:admin" + (
            ",monitor:viewer" if mode == "local" else "${VECTEN_AIRFLOW_VIEWERS:-}"
        )
        if mode == "local":
            refs = json.loads(
                services["airflow-auth"]["environment"]["VECTEN_AIRFLOW_API_ACCOUNTS"]
            )
            env = {
                "AIRFLOW_ADMIN_USER": "admin",
                "AIRFLOW_ADMIN_PASSWORD": "a",
                "MONITOR_PASSWORD": "b",
            }
            assert passwords(env, refs) == {"admin": "a", "monitor": "b"}
            del env["MONITOR_PASSWORD"]
            with pytest.raises(ValueError, match="MONITOR_PASSWORD"):
                passwords(env, refs)
    with pytest.raises(Failure, match="duplicate Airflow pools"):
        effective_resources(config, {"extra": Extension("1", (), airflow=config.airflow)})
    with pytest.raises(Failure, match="needs its own"):
        effective_resources(config, {"extra": Extension("1", (), airflow=manifest.airflow)})
