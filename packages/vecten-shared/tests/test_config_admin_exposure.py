import pytest
from pydantic import ValidationError

from vecten_shared.config import VectenConfig

PROJECT = {"users": 1, "project": {"name": "a", "scaffold_version": "0.1.0"}}


def test_admin_panel_exposure_defaults_to_public_without_allow_list():
    cfg = VectenConfig.model_validate(PROJECT)
    assert cfg.mcp.admin_panel == "public" and cfg.mcp.admin_allow == []


def test_admin_allow_list_normalises_networks():
    cfg = VectenConfig.model_validate(
        {**PROJECT, "mcp": {"admin_allow": ["203.0.113.7", "2001:db8::/32", "198.51.100.0/24"]}}
    )
    assert cfg.mcp.admin_allow == ["203.0.113.7/32", "2001:db8::/32", "198.51.100.0/24"]


@pytest.mark.parametrize(
    "mcp",
    [
        {"admin_allow": ["not-a-network"]},
        {"admin_allow": ["10.0.0.1/8"]},
        {"admin_allow": ["10.0.0.0/8; allow all"]},
        {"admin_panel": "private"},
        {"admin_panel": "off", "admin_allow": ["10.0.0.0/8"]},
    ],
)
def test_admin_exposure_refuses_invalid_values(mcp):
    with pytest.raises(ValidationError):
        VectenConfig.model_validate({**PROJECT, "mcp": mcp})
