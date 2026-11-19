import tomllib

import pytest
from pydantic import ValidationError

from vecten_shared.config import VectenConfig, load_config, save_config
from vecten_shared.output import ExitCode, Failure

PROJECT = {"users": 1, "project": {"name": "a", "scaffold_version": "0.1.0"}}


def test_airflow_allow_list_defaults_to_empty():
    assert VectenConfig.model_validate(PROJECT).airflow.allow == []


def test_airflow_allow_list_normalises_networks_as_admin_allow_does():
    networks = ["203.0.113.7", "2001:db8::/32", "198.51.100.0/24"]
    cfg = VectenConfig.model_validate(
        {**PROJECT, "airflow": {"allow": networks}, "mcp": {"admin_allow": networks}}
    )
    assert cfg.airflow.allow == ["203.0.113.7/32", "2001:db8::/32", "198.51.100.0/24"]
    assert cfg.airflow.allow == cfg.mcp.admin_allow


@pytest.mark.parametrize(
    "airflow",
    [
        {"allow": ["not-a-network"]},
        {"allow": ["10.0.0.1/8"]},
        {"allow": ["10.0.0.0/8; allow all"]},
        {"allow": "10.0.0.0/8"},
        {"allow_list": ["10.0.0.0/8"]},
    ],
)
def test_airflow_allow_list_refuses_invalid_values(airflow):
    with pytest.raises(ValidationError):
        VectenConfig.model_validate({**PROJECT, "airflow": airflow})


def test_airflow_allow_list_needs_the_platform_component():
    with pytest.raises(ValidationError, match="needs the platform component"):
        VectenConfig.model_validate(
            {**PROJECT, "components": ["mcp"], "airflow": {"allow": ["10.0.0.0/8"]}}
        )
    cfg = VectenConfig.model_validate({**PROJECT, "components": ["mcp"]})
    assert cfg.airflow.allow == []


def test_airflow_allow_list_round_trips_through_vecten_toml(tmp_path):
    (tmp_path / "vecten.toml").write_text(
        'users = 1\n[project]\nname = "a"\nscaffold_version = "0.1.0"\n'
        '[airflow]\nallow = ["192.0.2.1"]\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.airflow.allow == ["192.0.2.1/32"]
    save_config(tmp_path, cfg)
    assert tomllib.loads((tmp_path / "vecten.toml").read_text())["airflow"] == {
        "allow": ["192.0.2.1/32"]
    }


def test_invalid_airflow_allow_list_in_vecten_toml_is_a_config_error(tmp_path):
    (tmp_path / "vecten.toml").write_text(
        'users = 1\n[project]\nname = "a"\nscaffold_version = "0.1.0"\n'
        '[airflow]\nallow = ["10.0.0.1/8"]\n'
    )
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG
    assert "airflow.allow" in exc.value.message
