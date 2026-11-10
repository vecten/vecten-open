"""Which skill, command or question resolves a platform failure."""

import pytest

from vecten.issues.resolutions import airflow_resolution
from vecten_shared.config import VectenConfig

SYNC = "sync_hubspot_default"


def config(secrets=("HUBSPOT_TOKEN",)):
    return VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "acme", "scaffold_version": "0.1.0"},
            "connectors": [{"name": "hubspot", "source": "custom", "secrets": list(secrets)}],
        }
    )


def test_a_source_bug_goes_to_the_connector_skill():
    route = airflow_resolution(config(), "prod", SYNC, "KeyError", "hubspot.deals: KeyError: 'x'")
    assert (route.kind, route.resolution) == ("skill", "connector")


def test_a_missing_secret_is_a_secret_set_command():
    error = "secret HUBSPOT_TOKEN is not set in prod; run `vecten secret set HUBSPOT_TOKEN`"
    route = airflow_resolution(config(), "prod", SYNC, "Failure", error)
    assert route.kind == "command"
    assert route.resolution == "vecten secret set HUBSPOT_TOKEN --env prod"


@pytest.mark.parametrize(
    "error",
    [
        "hubspot.deals: HTTPError: 401 Client Error: Unauthorized for url",
        "hubspot.deals: HTTPError: 403 Client Error: Forbidden",
        "hubspot.deals: ValueError: Invalid API key provided",
        "hubspot.deals: AuthenticationError: authentication failed",
    ],
)
def test_a_revoked_credential_is_a_secret_set_command(error):
    route = airflow_resolution(config(), "prod", SYNC, "HTTPError", error)
    assert route.kind == "command"
    assert route.resolution == "vecten secret set HUBSPOT_TOKEN --env prod"


def test_on_the_laptop_the_secret_goes_to_env_without_an_environment():
    route = airflow_resolution(config(), "local", SYNC, "HTTPError", "401 Unauthorized")
    assert route.resolution == "vecten secret set HUBSPOT_TOKEN"


def test_a_connector_with_several_secrets_names_them_for_the_agent_to_choose():
    route = airflow_resolution(
        config(("HUBSPOT_TOKEN", "HUBSPOT_PORTAL")), "prod", SYNC, "HTTPError", "401"
    )
    assert route.resolution == "vecten secret set <NAME> --env prod"
    assert "HUBSPOT_PORTAL" in route.detail and "HUBSPOT_TOKEN" in route.detail


def test_an_auth_failure_of_a_connector_without_secrets_goes_to_the_skill():
    route = airflow_resolution(config(()), "prod", SYNC, "HTTPError", "401 Unauthorized")
    assert (route.kind, route.resolution) == ("skill", "connector")


@pytest.mark.parametrize(
    ("error_class", "error"),
    [
        ("HTTPError", "hubspot.deals: HTTPError: 503 Server Error: Service Unavailable"),
        ("HTTPError", "hubspot.deals: HTTPError: 429 Client Error: Too Many Requests"),
        ("ConnectionError", "hubspot.deals: ConnectionError: Max retries exceeded"),
        ("ReadTimeout", "hubspot.deals: ReadTimeout: read timed out"),
    ],
)
def test_an_upstream_outage_is_a_question_to_relay(error_class, error):
    route = airflow_resolution(config(), "prod", SYNC, error_class, error)
    assert route.kind == "ask"
    assert "hubspot" in route.resolution and "next green run" in route.resolution


def test_the_dbt_dag_goes_to_the_model_skill():
    route = airflow_resolution(config(), "prod", "dbt_build", "RuntimeError", "dbt deps failed")
    assert (route.kind, route.resolution) == ("skill", "model")


def test_a_dag_vecten_did_not_render_is_a_question_for_its_owner():
    route = airflow_resolution(config(), "prod", "custom_export", "RuntimeError", "x")
    assert route.kind == "ask" and "custom_export" in route.resolution
