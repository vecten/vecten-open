"""What `ship` reports for each bound service once its hosts are shipped."""

import copy

from vecten.deploy import shipping
from vecten_shared.config import load_config
from vecten_shared.extension_record import read_record
from vecten_shared.provider.environment import EnvironmentOutputs

from .deploy_fixtures import STANDARD_OUTPUTS, render_aws
from .services_fixtures import BOT, with_services

HEALTH = "https://bot.data.acme.com/_vecten/health"


def report(tmp_path, monkeypatch, declared, answers):
    root = render_aws(tmp_path / "proj")
    binding = {"hostname": "bot", "routes": {"webhook": {"access": "public"}}}
    with_services(root, {"bot": binding}, declared={"bot": declared})
    outputs = copy.deepcopy(STANDARD_OUTPUTS)
    outputs["hosts"]["platform"]["service_hostnames"] = ["bot.data.acme.com"]
    monkeypatch.setattr(shipping, "fetch_response", answers.get)
    monkeypatch.setattr(shipping, "SERVICE_WINDOW", 0)
    return shipping.service_report(
        load_config(root), read_record(root), EnvironmentOutputs.model_validate(outputs)
    )


def test_a_served_service_whose_health_is_a_command_is_reported_served(tmp_path, monkeypatch):
    declared = {**BOT, "health": {"command": ["true"]}}
    found = report(tmp_path, monkeypatch, declared, {})
    assert found == {"services": {"acme.bot": "served"}, "waiting_routes": []}


def test_a_redirect_from_the_health_path_passes_as_the_compose_check_does(tmp_path, monkeypatch):
    found = report(tmp_path, monkeypatch, BOT, {HEALTH: (302, "")})
    assert found["services"] == {"acme.bot": "healthy"}


def test_a_client_error_from_the_health_path_is_unhealthy(tmp_path, monkeypatch):
    found = report(tmp_path, monkeypatch, BOT, {HEALTH: (404, "")})
    assert found["services"] == {"acme.bot": "unhealthy"}
