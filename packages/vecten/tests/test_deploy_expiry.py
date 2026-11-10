"""Key expiries bootstrap recorded, as `next` and `deploy` read them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vecten.deploy.expiry import RENEW_WITHIN, expiring_keys, renewal_warning
from vecten.next.deploy import key_handover_items, key_renewal_items
from vecten_shared.config import DeploySection, EnvironmentFacts, VectenConfig

NOW = datetime(2027, 9, 1, 12, 0, tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _facts(**expiries: timedelta) -> EnvironmentFacts:
    ids = {"project": "p", **{f"expires_{k}": _stamp(NOW + v) for k, v in expiries.items()}}
    return EnvironmentFacts(bootstrapped=True, cloud_ids=ids)


def test_the_window_is_the_one_bootstrap_rotates_in():
    from vecten.deploy.scaleway.bootstrap import ROTATE_WITHIN

    assert RENEW_WITHIN == ROTATE_WITHIN == timedelta(days=30)


def test_keys_expiring_within_30_days_are_named_by_owner():
    facts = _facts(deploy=timedelta(days=29), plan=timedelta(days=31), mcp=timedelta(days=-1))
    found = expiring_keys(facts, NOW)
    assert sorted(found) == ["deploy", "mcp"]
    assert found["deploy"] == NOW + timedelta(days=29)


def test_no_recorded_expiry_warns_of_nothing():
    assert expiring_keys(EnvironmentFacts(), NOW) == {}
    assert expiring_keys(None, NOW) == {}


def test_the_warning_names_each_key_its_date_and_bootstrap():
    text = renewal_warning("prod", {"deploy": NOW + timedelta(days=3)})
    assert "deploy" in text and "2027-09-04" in text
    assert "vecten bootstrap --env prod" in text


def _config(facts: EnvironmentFacts) -> VectenConfig:
    config = VectenConfig.model_validate(
        {"users": 1, "project": {"name": "a", "scaffold_version": "0.1.0"}}
    )
    config.environments["prod"] = facts
    return config


def test_next_offers_bootstrap_when_a_key_expires_within_30_days():
    (item,) = key_renewal_items(_config(_facts(platform=timedelta(days=10))), "prod", NOW)
    assert item.kind == "command"
    assert item.resolution == "vecten bootstrap --env prod"
    assert "platform" in item.title and "2027-09-11" in item.title


def test_next_offers_nothing_while_every_key_has_more_than_30_days():
    config = _config(_facts(deploy=timedelta(days=200), plan=timedelta(days=31)))
    assert key_renewal_items(config, "prod", NOW) == []


def test_an_environment_never_bootstrapped_has_nothing_to_renew():
    facts = _facts(deploy=timedelta(days=1))
    facts.bootstrapped = False
    assert key_renewal_items(_config(facts), "prod", NOW) == []


def _scaleway(facts: EnvironmentFacts, cloud: str = "scaleway") -> VectenConfig:
    config = _config(facts)
    deploy = {
        "cloud": cloud,
        "region": "fr-par",
        "domain": "data.acme.com",
        "dns_zone": "acme.com",
        "github_repository": "acme/data",
    }
    return config.model_copy(update={"deploy": DeploySection.model_validate(deploy)})


def _handover(**ids: str) -> EnvironmentFacts:
    return EnvironmentFacts(
        bootstrapped=True, deployed_commit="a" * 40, cloud_ids={"project": "p", **ids}
    )


def test_next_asks_for_a_deploy_while_a_vm_key_waits_to_be_handed_over():
    facts = _handover(retiring_platform="SCWOLDPLATFORM", retiring_deploy="SCWOLDDEPLOY")
    (item,) = key_handover_items(_scaleway(facts), "prod")
    assert item.kind == "command"
    assert item.resolution == "vecten deploy --env prod"
    assert "platform" in item.title and "deploy key" not in item.title


def test_a_retiring_ci_key_needs_no_deploy():
    facts = _handover(retiring_deploy="SCWOLDDEPLOY", retiring_plan="SCWOLDPLAN")
    assert key_handover_items(_scaleway(facts), "prod") == []


def test_aws_and_undeployed_environments_hand_nothing_over():
    assert key_handover_items(_scaleway(_handover(retiring_mcp="x"), "aws"), "prod") == []
    undeployed = _handover(retiring_mcp="SCWOLDMCP")
    undeployed.deployed_commit = None
    assert key_handover_items(_scaleway(undeployed), "prod") == []
