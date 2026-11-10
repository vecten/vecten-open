import pytest

from vecten.connectors.names import check_connector_name, secret_name
from vecten_shared.output import ExitCode, Failure


@pytest.mark.parametrize("name", ["hubspot", "my_source2", "a"])
def test_accepts_lowercase_underscore_names(name):
    check_connector_name(name)


@pytest.mark.parametrize(
    "name", ["Hub-Spot", "hub-spot", "2fast", "_x", "", "a" * 64, "raw schema"]
)
def test_refuses_names_that_would_break_the_schema_or_package(name):
    with pytest.raises(Failure) as exc:
        check_connector_name(name)
    assert exc.value.code == ExitCode.USAGE
    assert "lowercase" in exc.value.message


def test_secret_name_is_upper_snake():
    assert secret_name("hubspot", "api_key") == "HUBSPOT_API_KEY"
    assert secret_name("billing", "client-secret") == "BILLING_CLIENT_SECRET"
