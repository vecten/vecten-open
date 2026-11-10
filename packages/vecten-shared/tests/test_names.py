import pytest

from vecten_shared.names import (
    check_connector_name,
    check_env_name,
    check_group_name,
    check_secret_name,
)
from vecten_shared.output import ExitCode, Failure


@pytest.mark.parametrize(
    "check,good,bad",
    [
        (check_connector_name, "hubspot_2", "Hub-Spot"),
        (check_group_name, "sales_ops", "sales-ops"),
        (check_secret_name, "HUBSPOT_API_KEY", "HUBSPOT-KEY"),
        (check_env_name, "prod-eu", "Prod"),
    ],
)
def test_each_rule_accepts_its_shape_and_refuses_as_usage(check, good, bad):
    check(good)
    with pytest.raises(Failure) as exc:
        check(bad)
    assert exc.value.code == ExitCode.USAGE and repr(bad) in exc.value.message


@pytest.mark.parametrize("bad", ["prod-", "prod--eu", "-prod", "1prod", "p" * 33])
def test_env_names_refuse_hyphens_that_would_collide_on_resource_names(bad):
    with pytest.raises(Failure) as exc:
        check_env_name(bad)
    assert exc.value.code == ExitCode.USAGE and repr(bad) in exc.value.message
    assert "no hyphen at the end or next to another" in exc.value.message


@pytest.mark.parametrize("good", ["prod", "prod-eu", "staging-2", "a", "p" * 32, "a-b-c"])
def test_env_names_with_single_inner_hyphens_pass(good):
    check_env_name(good)


def test_local_is_reserved_as_an_environment_name():
    with pytest.raises(Failure) as exc:
        check_env_name("local")
    assert exc.value.code == ExitCode.USAGE and "reserved" in exc.value.message
