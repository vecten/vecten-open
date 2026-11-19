"""`vecten.apps` declarations: what an analytics app is, reads and runs as."""

import json

import pytest

from vecten_shared.extensions import GROUPS
from vecten_shared.output import ExitCode, Failure

STREAMLIT = {
    "title": "Sales overview",
    "description": "Weekly bookings by region",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme_sales/app.py",
    "datasets": ["orders", "customers"],
    "groups": ["default"],
}
GENERIC = {
    "title": "Board",
    "description": "",
    "kind": "container",
    "command": ["python", "-m", "acme_sales.board"],
    "port": 9000,
    "datasets": ["orders"],
    "groups": ["sales"],
}
SNOWFLAKE = {
    "title": "Finance",
    "description": "Revenue in Snowflake",
    "kind": "snowflake",
    "entry": "acme_sales/finance.py",
    "datasets": ["revenue"],
    "groups": ["finance"],
}


def parse(value, name="overview"):
    from vecten_shared.apps import parse_app

    return parse_app("acme-sales", name, value)


def test_apps_is_an_extension_group():
    assert "vecten.apps" in GROUPS


def test_a_streamlit_app_generates_its_service():
    from vecten_shared.services import Route

    declared = parse(STREAMLIT)
    app = declared.app
    assert declared.unknown == ()
    assert (app.title, app.description, app.kind) == (
        "Sales overview",
        "Weekly bookings by region",
        "container",
    )
    assert app.datasets == ("orders", "customers")
    assert app.groups == ("default",)
    assert (app.runtime, app.entry) == ("streamlit", "acme_sales/app.py")
    service = app.service
    assert service.command == (
        "python",
        "-m",
        "vecten_shared.launch",
        "streamlit",
        "acme_sales/app.py",
    )
    assert service.health_path == "/_stcore/health" and service.health_command is None
    assert service.port == 8501
    assert service.volume is None
    assert service.components == ("platform",)
    assert (service.memory_mb, service.cpus) == (512, 0.5)
    assert service.routes == {"app": Route("/", "login", ("default",), True, 1)}


def test_a_generic_container_app_gives_its_own_command_and_port():
    service = parse(GENERIC).app.service
    assert service.command == ("python", "-m", "acme_sales.board")
    assert service.port == 9000
    assert service.health_path == "/"
    assert service.routes["app"].groups == ("sales",)


def test_a_generic_app_may_give_health_components_and_limits():
    value = {
        **GENERIC,
        "health": {"path": "/ping"},
        "components": ["mcp", "platform"],
        "memory_mb": 1024,
        "cpus": 1,
    }
    service = parse(value).app.service
    assert service.health_path == "/ping"
    assert service.components == ("mcp", "platform")
    assert (service.memory_mb, service.cpus) == (1024, 1.0)


def test_a_snowflake_app_has_no_service():
    app = parse(SNOWFLAKE).app
    assert app.kind == "snowflake" and app.service is None
    assert app.entry == "acme_sales/finance.py"


def test_a_factory_is_called():
    assert parse(lambda: STREAMLIT).app.title == "Sales overview"


def test_unknown_keys_are_reported_not_refused():
    assert parse({**STREAMLIT, "theme": "dark"}).unknown == ("theme",)
    assert parse({**GENERIC, "health": {"path": "/", "every": 3}}).unknown == ("health.every",)


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"datasets": []}, "datasets"),
        ({"datasets": ["orders", "orders"]}, "datasets"),
        ({"datasets": ["no spaces"]}, "datasets"),
        ({"datasets": "orders"}, "datasets"),
        ({"groups": []}, "groups"),
        ({"groups": ["Sales"]}, "groups"),
        ({"title": ""}, "title"),
        ({"title": None}, "title"),
        ({"description": 3}, "description"),
        ({"kind": "lambda"}, "kind"),
        ({"runtime": "dash"}, "runtime"),
        ({"entry": None}, "entry"),
        ({"entry": "/abs/app.py"}, "entry"),
        ({"entry": "app.py"}, "entry"),
        ({"entry": "acme_sales/../other/app.py"}, "entry"),
        ({"entry": "acme_sales/./app.py"}, "entry"),
        ({"entry": "acme_sales//app.py"}, "entry"),
        ({"entry": "acme-sales/app.py"}, "entry"),
        ({"entry": "acme_sales\\app.py"}, "entry"),
        ({"entry": "acme_sales/app.txt"}, "entry"),
        ({"command": ["run"]}, "command"),
        ({"port": 80}, "port"),
        ({"health": {"path": "/x"}}, "health"),
        ({"memory_mb": 10}, "memory_mb"),
        ({"cpus": 0}, "cpus"),
        ({"components": ["cli"]}, "components"),
    ],
)
def test_a_malformed_streamlit_app_names_the_extension_the_app_and_the_key(change, key):
    with pytest.raises(Failure) as caught:
        parse({**STREAMLIT, **change})
    assert caught.value.code == ExitCode.CONFIG
    assert caught.value.message.startswith(f"extension acme-sales: app overview: {key}:")


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"command": None}, "command"),
        ({"port": None}, "port"),
        ({"port": 70000}, "port"),
        ({"entry": "acme_sales/app.py"}, "entry"),
        ({"health": {"path": "/_vecten/x/../y"}}, "health"),
    ],
)
def test_a_malformed_generic_app_names_the_key(change, key):
    value = {k: v for k, v in {**GENERIC, **change}.items() if v is not None}
    with pytest.raises(Failure, match=f"^extension acme-sales: app overview: {key}:"):
        parse(value)


@pytest.mark.parametrize("key", ["runtime", "command", "port", "health", "components", "memory_mb"])
def test_a_snowflake_app_refuses_what_only_a_container_runs(key):
    value = {**SNOWFLAKE, key: STREAMLIT.get(key) or GENERIC.get(key) or 1}
    with pytest.raises(Failure, match=f"app overview: {key}: only a container app"):
        parse(value)


def test_a_snowflake_app_needs_its_main_file():
    value = {k: v for k, v in SNOWFLAKE.items() if k != "entry"}
    with pytest.raises(Failure, match="app overview: entry:"):
        parse(value)


@pytest.mark.parametrize("name", ["Overview", "1st", "a" * 32, "with_underscore"])
def test_an_app_name_follows_the_service_rule(name):
    with pytest.raises(Failure, match="app"):
        parse(STREAMLIT, name=name)


def test_a_non_mapping_and_a_raising_factory_are_named():
    with pytest.raises(Failure, match="app overview: must be a mapping"):
        parse(["x"])

    def broken():
        raise KeyError("x")

    with pytest.raises(Failure, match="app overview: its factory raised KeyError"):
        parse(broken)


def test_the_record_holds_apps_and_reads_them_back():
    from vecten_shared.extension_record import Extension, parse_record, record_text

    apps = {
        "overview": parse(STREAMLIT).app,
        "board": parse(GENERIC, "board").app,
        "finance": parse(SNOWFLAKE, "finance").app,
    }
    text = record_text({"acme-sales": Extension("1.0", (), apps=apps)})
    body = json.loads(text)["extensions"]["acme-sales"]["apps"]
    assert body["overview"] == {
        "title": "Sales overview",
        "description": "Weekly bookings by region",
        "kind": "container",
        "runtime": "streamlit",
        "entry": "acme_sales/app.py",
        "datasets": ["orders", "customers"],
        "groups": ["default"],
        "components": ["platform"],
        "memory_mb": 512,
        "cpus": 0.5,
    }
    assert body["board"]["command"] == ["python", "-m", "acme_sales.board"]
    assert body["board"]["health"] == {"path": "/"}
    assert "port" not in body["finance"] and "components" not in body["finance"]
    assert parse_record(text)["acme-sales"].apps == apps


def test_a_record_without_apps_holds_no_apps_key():
    from vecten_shared.extension_record import Extension, parse_record, record_text

    text = record_text({"plain": Extension("1.0", ())})
    assert '"apps"' not in text
    assert parse_record(text)["plain"].apps == {}


def test_a_record_with_a_malformed_app_is_not_trusted():
    from vecten_shared.extension_record import Extension, parse_record, record_text

    entry = Extension("1.0", (), apps={"overview": parse(STREAMLIT).app})
    body = json.loads(record_text({"acme-sales": entry}))
    body["extensions"]["acme-sales"]["apps"]["overview"]["entry"] = "../etc/passwd.py"
    assert parse_record(json.dumps(body)) is None
    body["extensions"]["acme-sales"]["apps"] = {"Bad": {}}
    assert parse_record(json.dumps(body)) is None


def test_the_app_service_and_a_declared_service_of_that_name_collide():
    from vecten_shared.apps import app_service_name, colliding_service

    assert app_service_name("overview") == "app-overview"
    assert colliding_service({"app-overview": object()}, {"overview": object()}) == "app-overview"
    assert colliding_service({"overview": object()}, {"overview": object()}) is None


def test_credential_names_follow_the_host_and_the_app_name():
    from vecten_shared.apps import credential_names
    from vecten_shared.declarations import component_for

    assert credential_names("sales-overview", "platform") == (
        "APP_SALES_OVERVIEW_CLIENT_ID",
        "APP_SALES_OVERVIEW_CLIENT_SECRET",
    )
    mcp = credential_names("overview", "mcp")
    assert mcp == ("MCP_APP_OVERVIEW_CLIENT_ID", "MCP_APP_OVERVIEW_CLIENT_SECRET")
    assert {component_for(n) for n in mcp} == {"mcp"}
    assert {component_for(n) for n in credential_names("overview", "platform")} == {"platform"}


@pytest.mark.parametrize(
    "name",
    [
        "APP_OVERVIEW_CLIENT_ID",
        "APP_OVERVIEW_CLIENT_SECRET",
        "MCP_APP_SALES_X_CLIENT_SECRET",
    ],
)
def test_an_app_credential_name_is_reserved_from_declarations(name):
    from vecten_shared.declarations import secret_problem

    assert "analytics app" in secret_problem(name)


def test_other_app_like_names_stay_declarable():
    from vecten_shared.declarations import secret_problem

    assert secret_problem("APP_TOKEN") is None
    assert secret_problem("MCP_APP_TOKEN") is None
