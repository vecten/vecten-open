"""`[services.<extension>.<service>]` in vecten.toml: refusals on load and against the record."""

import pytest
from pydantic import ValidationError

from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import Extension
from vecten_shared.output import ExitCode, Failure
from vecten_shared.services import bound_services, check_bindings, parse_service

BOT = {
    "command": ["python", "-m", "acme_bot.server"],
    "port": 8000,
    "health": {"path": "/healthz"},
    "routes": {
        "webhook": {"path": "/api/messages", "access": "public"},
        "admin": {"path": "/", "access": "login", "groups": ["default"]},
    },
}


def config(services, **extra):
    return VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "extensions": ["acme-bot"],
            "services": services,
            **extra,
        }
    )


def record(**changes):
    service = parse_service("acme-bot", "bot", {**BOT, **changes}).service
    return {"acme-bot": Extension("1.0", (), services={"bot": service})}


BOUND = {
    "acme-bot": {
        "bot": {
            "hostname": "bot",
            "routes": {
                "webhook": {"access": "public"},
                "admin": {"access": "login", "groups": ["default"], "allow": ["203.0.113.7"]},
            },
        }
    }
}


def test_a_binding_loads_with_defaults_and_normalised_networks():
    loaded = config(BOUND)
    binding = loaded.services["acme-bot"]["bot"]
    assert binding.local is True and binding.host is None
    assert binding.memory_mb is None and binding.cpus is None
    assert binding.routes["admin"].allow == ["203.0.113.7/32"]


def test_a_project_without_services_has_none():
    assert config({}).services == {}


def _refused(services, said, **extra):
    with pytest.raises(ValidationError) as caught:
        config(services, **extra)
    assert said in str(caught.value)


@pytest.mark.parametrize(
    ("binding", "said"),
    [
        ({"hostname": "mcp"}, "reserved"),
        ({"hostname": "airflow"}, "reserved"),
        ({"hostname": "www"}, "reserved"),
        ({"hostname": "_gate"}, "reserved"),
        ({"hostname": "Bot"}, "hostname"),
        ({"hostname": "bot.example"}, "hostname"),
        ({"hostname": "-bot"}, "hostname"),
        ({"routes": {"webhook": {"access": "public"}}}, "hostname is required"),
        ({"hostname": "bot", "routes": {"webhook": {"access": "open"}}}, "access"),
        ({"hostname": "bot", "routes": {"admin": {"access": "login"}}}, "groups"),
        (
            {"hostname": "bot", "routes": {"webhook": {"access": "public", "groups": ["x"]}}},
            "groups",
        ),
        (
            {"hostname": "bot", "routes": {"admin": {"access": "login", "groups": ["Bad"]}}},
            "groups",
        ),
        (
            {"hostname": "bot", "routes": {"webhook": {"access": "public", "allow": ["nope"]}}},
            "allow",
        ),
        ({"hostname": "bot", "routes": {"Hook": {"access": "public"}}}, "route"),
        ({"memory_mb": 16}, "memory_mb"),
        ({"cpus": 0}, "cpus"),
        ({"host": "cli"}, "host"),
        ({"replicas": 2}, "replicas"),
    ],
)
def test_a_malformed_binding_is_refused_on_load_naming_the_key(binding, said):
    _refused({"acme-bot": {"bot": binding}}, said)


def test_a_binding_for_an_unlisted_extension_is_refused():
    _refused({"other": {"bot": {}}}, "services.other: no extension of that name is listed")


def test_a_service_name_outside_the_rule_is_refused():
    _refused({"acme-bot": {"Bot": {}}}, "services.acme-bot.Bot")


def test_a_label_bound_twice_is_refused():
    services = {"acme-bot": {"bot": {"hostname": "bot"}, "portal": {"hostname": "bot"}}}
    _refused(services, "services.acme-bot.portal.hostname: bot is bound twice")


def test_login_needs_the_mcp_component():
    _refused(
        BOUND, "routes.admin.access: a login route needs the mcp component", components=["platform"]
    )


def test_a_host_the_project_does_not_enable_is_refused():
    _refused(
        {"acme-bot": {"bot": {"host": "mcp"}}},
        "services.acme-bot.bot.host",
        components=["platform"],
    )


DEPLOY = {"cloud": "aws", "region": "eu-west-1", "github_repository": "acme/data"}


def test_the_apex_is_refused_where_the_domain_is_the_zone():
    deploy = {**DEPLOY, "domain": "acme.com", "dns_zone": "acme.com"}
    _refused({"acme-bot": {"bot": {"hostname": "@"}}}, "hostname: @", deploy=deploy)


def test_the_apex_is_bound_below_the_zone():
    deploy = {**DEPLOY, "domain": "data.acme.com", "dns_zone": "acme.com"}
    loaded = config({"acme-bot": {"bot": {"hostname": "@"}}}, deploy=deploy)
    assert loaded.services["acme-bot"]["bot"].hostname == "@"


def _check_refused(services, rec, said, **extra):
    with pytest.raises(Failure) as caught:
        check_bindings(config(services, **extra), rec)
    assert caught.value.code == ExitCode.CONFIG
    assert said in caught.value.message


def test_bindings_check_against_the_record():
    check_bindings(config(BOUND), record())


def test_an_extension_missing_from_the_record_names_vecten_lock():
    _check_refused(BOUND, {}, "services.acme-bot: acme-bot is not in extensions.lock.json")


def test_an_undeclared_service_is_refused():
    _check_refused({"acme-bot": {"portal": {}}}, record(), "services.acme-bot.portal")


def test_an_undeclared_route_is_refused():
    services = {
        "acme-bot": {"bot": {"hostname": "bot", "routes": {"export": {"access": "public"}}}}
    }
    _check_refused(services, record(), "services.acme-bot.bot.routes.export")


def test_a_host_the_declaration_does_not_allow_is_refused():
    _check_refused({"acme-bot": {"bot": {"host": "mcp"}}}, record(), "services.acme-bot.bot.host")


def test_host_is_required_when_platform_is_not_an_option():
    _check_refused(
        {"acme-bot": {"bot": {}}},
        record(components=["mcp"]),
        "services.acme-bot.bot.host: required",
    )


def test_bound_services_resolve_host_limits_and_bound_routes():
    services = {
        "acme-bot": {
            "bot": {
                "hostname": "bot",
                "memory_mb": 512,
                "routes": {"webhook": {"access": "public"}},
            }
        }
    }
    [bound] = bound_services(config(services), record(cpus=1.5))
    assert (bound.extension, bound.name, bound.compose_name) == (
        "acme-bot",
        "bot",
        "svc-acme-bot-bot",
    )
    assert bound.host == "platform"
    assert (bound.memory_mb, bound.cpus) == (512, 1.5)
    assert set(bound.routes) == {"webhook"}
    assert bound.routes["webhook"].path == "/api/messages"


def test_an_unbound_service_does_not_run():
    assert bound_services(config({}), record()) == []


def test_unbound_parts_are_reported():
    from vecten_shared.services import unbound_parts

    services = {
        "acme-bot": {"bot": {"hostname": "bot", "routes": {"webhook": {"access": "public"}}}}
    }
    assert unbound_parts(config(services), record()) == ["acme-bot.bot route admin"]
    assert unbound_parts(config({}), record()) == ["acme-bot.bot"]
