import pytest

from vecten_shared.config import (
    PROMOTION_SINCE,
    VectenConfig,
    load_config,
    promotion_scaffold_problem,
    save_config,
)
from vecten_shared.output import ExitCode, Failure

BASE = """
users = 10

[project]
name = "acme"
scaffold_version = "{version}"

[deploy]
cloud = "aws"
region = "eu-central-1"
domain = "data.acme.com"
dns_zone = "acme.com"
github_repository = "acme/data"
{promotion}
"""


def _write(root, promotion: str = "", version: str = PROMOTION_SINCE):
    text = BASE.format(version=version, promotion=promotion)
    (root / "vecten.toml").write_text(text)
    return root


def _config(promotion=None, version: str = PROMOTION_SINCE) -> VectenConfig:
    deploy = {
        "cloud": "aws",
        "region": "eu-central-1",
        "domain": "data.acme.com",
        "dns_zone": "acme.com",
        "github_repository": "acme/data",
    }
    if promotion is not None:
        deploy["promotion"] = promotion
    return VectenConfig.model_validate(
        {"users": 3, "project": {"name": "acme", "scaffold_version": version}, "deploy": deploy}
    )


def test_without_the_key_the_order_is_prod_alone(tmp_path):
    config = load_config(_write(tmp_path))
    assert config.deploy.promotion is None
    assert config.promotion_order() == ("prod",)
    assert config.promoted_from("prod") is None


def test_a_project_without_a_deploy_layer_orders_prod_alone():
    config = VectenConfig.model_validate(
        {"users": 3, "project": {"name": "acme", "scaffold_version": "0.1.0"}}
    )
    assert config.promotion_order() == ("prod",)


@pytest.mark.parametrize(
    "order", [["dev", "prod"], ["dev", "staging", "prod"], ["a", "b", "c", "d", "prod"]]
)
def test_a_chain_of_any_length_ending_in_prod_is_accepted(tmp_path, order):
    config = load_config(_write(tmp_path, f"promotion = {order!r}".replace("'", '"')))
    assert config.promotion_order() == tuple(order)


def test_each_environment_promotes_from_the_one_before_it():
    config = _config(["dev", "staging", "prod"])
    assert config.promoted_from("dev") is None
    assert config.promoted_from("staging") == "dev"
    assert config.promoted_from("prod") == "staging"
    # An environment outside the order ships by dispatch and promotes from nothing.
    assert config.promoted_from("sandbox") is None


@pytest.mark.parametrize(
    ("promotion", "problem"),
    [
        ('promotion = ["dev", "staging"]', "prod"),
        ('promotion = ["prod", "staging"]', "prod"),
        ('promotion = ["dev", "prod", "staging"]', "prod"),
        ('promotion = ["prod"]', "at least two"),
        ("promotion = []", "at least two"),
        ('promotion = ["dev", "dev", "prod"]', "dev more than once"),
        ('promotion = ["Dev", "prod"]', "environment name"),
        ('promotion = ["local", "prod"]', "reserved"),
    ],
)
def test_an_invalid_order_names_the_key_and_the_problem(tmp_path, promotion, problem):
    with pytest.raises(Failure) as exc:
        load_config(_write(tmp_path, promotion))
    assert exc.value.code == ExitCode.CONFIG
    assert "deploy.promotion" in exc.value.message and problem in exc.value.message


def test_an_order_survives_a_save_and_a_project_without_one_saves_byte_identical(tmp_path):
    ordered = tmp_path / "ordered"
    ordered.mkdir()
    _write(ordered, 'promotion = ["dev", "prod"]')
    config = load_config(ordered)
    config.environments["dev"].bootstrapped = True
    save_config(ordered, config)
    assert load_config(ordered).deploy.promotion == ["dev", "prod"]

    plain = tmp_path / "plain"
    plain.mkdir()
    _write(plain)
    before = (plain / "vecten.toml").read_text()
    save_config(plain, load_config(plain))
    assert (plain / "vecten.toml").read_text() == before


def test_the_scaffold_problem_names_vecten_update_for_an_older_scaffold():
    assert promotion_scaffold_problem(_config(["dev", "prod"])) is None
    assert promotion_scaffold_problem(_config(None, version="0.1.0")) is None
    problem = promotion_scaffold_problem(_config(["dev", "prod"], version="0.1.0"))
    assert problem is not None and "vecten update" in problem and "0.1.0" in problem


def test_save_refuses_to_write_an_order_under_an_older_scaffold(tmp_path):
    _write(tmp_path, version="0.1.0")
    before = (tmp_path / "vecten.toml").read_text()
    config = load_config(tmp_path)
    config.deploy.promotion = ["dev", "prod"]
    with pytest.raises(Failure) as exc:
        save_config(tmp_path, config)
    assert exc.value.code == ExitCode.REFUSED and "vecten update" in exc.value.message
    assert (tmp_path / "vecten.toml").read_text() == before
