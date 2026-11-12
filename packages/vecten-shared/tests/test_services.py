import pytest

from vecten_shared.extensions import GROUPS
from vecten_shared.output import ExitCode, Failure
from vecten_shared.services import (
    Route,
    Service,
    compose_service_name,
    parse_service,
    service_from_json,
    service_to_json,
)

BOT = {
    "command": ["python", "-m", "acme_bot.server"],
    "port": 8000,
    "health": {"path": "/healthz"},
    "routes": {
        "webhook": {"path": "/api/messages", "access": "public", "max_body_mb": 2},
        "admin": {"path": "/", "access": "login", "groups": ["default"], "websocket": True},
    },
}


def test_services_is_an_extension_group():
    assert "vecten.services" in GROUPS


def test_a_declaration_with_defaults():
    declared = parse_service("acme-bot", "bot", BOT)
    service = declared.service
    assert declared.unknown == ()
    assert service.command == ("python", "-m", "acme_bot.server")
    assert service.port == 8000
    assert service.health_path == "/healthz" and service.health_command is None
    assert service.volume is None
    assert service.components == ("platform",)
    assert (service.memory_mb, service.cpus) == (256, 0.5)
    assert service.routes["webhook"] == Route("/api/messages", "public", (), False, 2)
    assert service.routes["admin"] == Route("/", "login", ("default",), True, 1)


def test_a_factory_is_called():
    assert parse_service("acme-bot", "bot", lambda: BOT).service.port == 8000


def test_a_health_command_and_no_routes_need_no_port():
    service = parse_service(
        "acme-bot", "worker", {"command": ["run"], "health": {"command": ["true"]}}
    ).service
    assert service.port is None and service.health_command == ("true",)
    assert service.routes == {}


def test_unknown_keys_are_reported_not_refused():
    value = {**BOT, "replicas": 2, "routes": {"hook": {"path": "/h", "access": "public", "x": 1}}}
    assert parse_service("acme-bot", "bot", value).unknown == ("replicas", "routes.hook.x")


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"command": []}, "command"),
        ({"command": "python -m bot"}, "command"),
        ({"command": ["python", ""]}, "command"),
        ({"port": 0}, "port"),
        ({"port": True}, "port"),
        ({"port": "8000"}, "port"),
        ({"port": None}, "port"),
        ({"health": None}, "health"),
        ({"health": {}}, "health"),
        ({"health": {"path": "/a", "command": ["true"]}}, "health"),
        ({"health": {"path": "healthz"}}, "health"),
        ({"health": {"path": "/a b"}}, "health"),
        ({"health": {"command": []}}, "health"),
        ({"volume": "data"}, "volume"),
        ({"volume": "/"}, "volume"),
        ({"volume": "/var/../etc"}, "volume"),
        ({"components": []}, "components"),
        ({"components": ["cli"]}, "components"),
        ({"memory_mb": 16}, "memory_mb"),
        ({"memory_mb": 1.5}, "memory_mb"),
        ({"cpus": 0}, "cpus"),
        ({"cpus": "1"}, "cpus"),
        ({"routes": []}, "routes"),
        ({"routes": {"Hook": {"path": "/h", "access": "public"}}}, "routes.Hook"),
        ({"routes": {"hook": {"path": "/_vecten/hooks", "access": "public"}}}, "routes.hook.path"),
        ({"routes": {"hook": {"path": "/_vecten", "access": "public"}}}, "routes.hook.path"),
        ({"routes": {"hook": {"path": "api", "access": "public"}}}, "routes.hook.path"),
        ({"routes": {"hook": {"path": "/a/../b", "access": "public"}}}, "routes.hook.path"),
        ({"routes": {"hook": {"path": "/a;b", "access": "public"}}}, "routes.hook.path"),
        ({"routes": {"hook": {"path": "/h", "access": "open"}}}, "routes.hook.access"),
        ({"routes": {"hook": {"path": "/h", "access": "login"}}}, "routes.hook.groups"),
        (
            {"routes": {"hook": {"path": "/h", "access": "public", "groups": ["default"]}}},
            "routes.hook.groups",
        ),
        (
            {"routes": {"hook": {"path": "/h", "access": "login", "groups": ["Bad Group"]}}},
            "routes.hook.groups",
        ),
        (
            {"routes": {"hook": {"path": "/h", "access": "public", "websocket": "yes"}}},
            "routes.hook.websocket",
        ),
        (
            {"routes": {"hook": {"path": "/h", "access": "public", "max_body_mb": 101}}},
            "routes.hook.max_body_mb",
        ),
        (
            {
                "routes": {
                    "a": {"path": "/h", "access": "public"},
                    "b": {"path": "/h", "access": "public"},
                }
            },
            "routes.b.path",
        ),
    ],
)
def test_a_malformed_known_key_is_refused_naming_extension_service_and_key(change, key):
    with pytest.raises(Failure) as caught:
        parse_service("acme-bot", "bot", {**BOT, **change})
    assert caught.value.code == ExitCode.CONFIG
    assert caught.value.message.startswith(f"extension acme-bot: service bot: {key}:")


def test_routes_need_a_port():
    value = {k: v for k, v in BOT.items() if k != "port"}
    with pytest.raises(Failure, match="service bot: port: required"):
        parse_service("acme-bot", "bot", value)


@pytest.mark.parametrize("name", ["Bot", "1bot", "b" * 32, "bot_1", ""])
def test_a_service_name_outside_the_rule_is_refused(name):
    with pytest.raises(Failure, match=f"service {name!r}"):
        parse_service("acme-bot", name, BOT)


def test_a_declaration_that_is_not_a_mapping_is_refused():
    with pytest.raises(Failure, match="service bot: must be a mapping or return one"):
        parse_service("acme-bot", "bot", ["python"])


def test_the_record_form_round_trips():
    service = parse_service(
        "acme-bot", "bot", {**BOT, "volume": "/var/lib/acme-bot", "components": ["mcp", "platform"]}
    ).service
    assert service_from_json(service_to_json(service)) == service
    assert service_to_json(service)["components"] == ["mcp", "platform"]


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"command": "x"},
        {"command": ["x"], "health": {"path": "/h"}, "routes": {"r": {"path": "/"}}},
    ],
)
def test_a_malformed_record_form_raises_value_error(broken):
    with pytest.raises(ValueError):
        service_from_json(broken)


def test_compose_names_stay_within_sixty_three_characters():
    assert compose_service_name("acme-bot", "bot") == "svc-acme-bot-bot"
    long = compose_service_name("e" * 60, "s" * 31)
    assert len(long) == 63 and long.startswith("svc-eeee")
    assert long != compose_service_name("e" * 60, "s" * 30 + "t")


def test_service_is_frozen():
    service = parse_service("acme-bot", "bot", BOT).service
    assert isinstance(service, Service)
    with pytest.raises(AttributeError):
        service.port = 1


def test_the_lock_record_holds_services_and_reads_them_back():
    from vecten_shared.extension_record import Extension, parse_record, record_text

    service = parse_service("acme-bot", "bot", BOT).service
    record = {"acme-bot": Extension("1.0", (), services={"bot": service})}
    text = record_text(record)
    assert '"services"' in text
    assert parse_record(text)["acme-bot"].services == {"bot": service}


def test_a_record_without_services_is_unchanged_by_the_services_key():
    from vecten_shared.extension_record import Extension, parse_record, record_text

    text = record_text({"plain": Extension("1.0", ())})
    assert '"services"' not in text
    assert parse_record(text)["plain"].services == {}


def test_a_record_with_a_malformed_service_is_not_trusted():
    import json

    from vecten_shared.extension_record import Extension, parse_record, record_text

    service = parse_service("acme-bot", "bot", BOT).service
    body = json.loads(record_text({"acme-bot": Extension("1.0", (), services={"bot": service})}))
    body["extensions"]["acme-bot"]["services"]["bot"]["routes"]["admin"]["path"] = "/_vecten/x"
    assert parse_record(json.dumps(body)) is None
    body["extensions"]["acme-bot"]["services"] = {"Bad": {}}
    assert parse_record(json.dumps(body)) is None
