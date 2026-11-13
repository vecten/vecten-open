"""`[apps.<extension>.<app>]` in vecten.toml: refusals on load and against the record."""

import pytest
from pydantic import ValidationError

from vecten_shared.apps import parse_app
from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import Extension
from vecten_shared.output import ExitCode, Failure
from vecten_shared.services import bound_services, check_bindings, parse_service

OVERVIEW = {
    "title": "Sales overview",
    "description": "",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme_sales/app.py",
    "datasets": ["orders"],
    "groups": ["default"],
}
FINANCE = {
    "title": "Finance",
    "description": "",
    "kind": "snowflake",
    "entry": "acme_sales/finance.py",
    "datasets": ["revenue"],
    "groups": ["finance"],
}
PORTAL = {
    "command": ["run"],
    "port": 8000,
    "health": {"path": "/healthz"},
    "routes": {"home": {"path": "/", "access": "login", "groups": ["default"]}},
}


def config(apps, **extra):
    return VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "extensions": ["acme-sales", "acme-other"],
            "apps": apps,
            **extra,
        }
    )


def record(**apps):
    declared = apps or {"overview": OVERVIEW, "finance": FINANCE}
    parsed = {name: parse_app("acme-sales", name, value).app for name, value in declared.items()}
    return {"acme-sales": Extension("1.0", (), apps=parsed)}


def test_a_binding_loads_with_defaults():
    binding = config({"acme-sales": {"overview": {"groups": ["sales"]}}}).apps["acme-sales"][
        "overview"
    ]
    assert binding.groups == ["sales"]
    assert binding.hostname is None and binding.local is None


def test_a_project_without_apps_has_none():
    assert config({}).apps == {}


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        ({}, "groups"),
        ({"groups": []}, "groups"),
        ({"groups": ["Sales"]}, "group names"),
        ({"groups": ["sales"], "hostname": "mcp"}, "reserved"),
        ({"groups": ["sales"], "hostname": "Not A Label"}, "DNS label"),
        ({"groups": ["sales"], "host": "mcp"}, "host"),
        ({"groups": ["sales"], "routes": {}}, "routes"),
    ],
)
def test_a_malformed_binding_is_refused_on_load(binding, message):
    with pytest.raises(ValidationError, match=message):
        config({"acme-sales": {"overview": binding}})


def test_a_binding_for_an_unlisted_extension_is_refused():
    with pytest.raises(ValidationError, match="apps.acme-unknown: no extension"):
        config({"acme-unknown": {"overview": {"groups": ["sales"]}}})


def test_an_app_name_that_breaks_the_rule_is_refused():
    with pytest.raises(ValidationError, match="apps.acme-sales.Overview"):
        config({"acme-sales": {"Overview": {"groups": ["sales"]}}})


def test_any_app_binding_needs_the_mcp_component():
    with pytest.raises(ValidationError, match="apps.acme-sales.finance: .*mcp component"):
        config({"acme-sales": {"finance": {"groups": ["finance"]}}}, components=["platform"])


def test_an_app_hostname_shared_with_a_service_is_refused():
    services = {"acme-other": {"portal": {"hostname": "sales", "routes": {}}}}
    with pytest.raises(ValidationError, match="apps.acme-sales.overview.hostname: sales is bound"):
        config(
            {"acme-sales": {"overview": {"groups": ["sales"], "hostname": "sales"}}},
            services=services,
        )


def test_two_apps_of_one_name_in_two_extensions_are_refused():
    apps = {
        "acme-sales": {"overview": {"groups": ["sales"], "hostname": "a"}},
        "acme-other": {"overview": {"groups": ["sales"], "hostname": "b"}},
    }
    with pytest.raises(ValidationError, match="svc_app_overview"):
        config(apps)


def test_the_apex_is_refused_where_the_domain_is_the_zone():
    deploy = {
        "cloud": "aws",
        "region": "eu-west-1",
        "domain": "acme.com",
        "dns_zone": "acme.com",
        "github_repository": "acme/data",
    }
    with pytest.raises(ValidationError, match="apps.acme-sales.overview.hostname: @"):
        config({"acme-sales": {"overview": {"groups": ["s"], "hostname": "@"}}}, deploy=deploy)


def test_a_container_app_binds_as_its_service_with_one_login_route():
    from vecten_shared.config import RouteBinding

    [bound] = bound_services(
        config({"acme-sales": {"overview": {"groups": ["sales", "finance"]}}}), record()
    )
    assert bound.name == "app-overview" and bound.app == "overview"
    assert bound.key == "acme-sales.app-overview"
    assert bound.compose_name == "svc-acme-sales-app-overview"
    assert bound.host == "platform"
    assert (bound.memory_mb, bound.cpus) == (512, 0.5)
    assert bound.binding.hostname == "overview" and bound.binding.local is True
    assert set(bound.routes) == {"app"}
    assert bound.routes["app"].path == "/" and bound.routes["app"].websocket is True
    assert bound.route_bindings == {
        "app": RouteBinding(access="login", groups=["sales", "finance"])
    }


def test_a_bound_hostname_and_local_are_kept():
    apps = {"acme-sales": {"overview": {"groups": ["sales"], "hostname": "@", "local": False}}}
    [bound] = bound_services(config(apps), record())
    assert bound.binding.hostname == "@" and bound.binding.local is False


def test_an_app_only_the_mcp_runs_is_placed_there():
    declared = record(overview={**OVERVIEW, "components": ["mcp"]})
    [bound] = bound_services(config({"acme-sales": {"overview": {"groups": ["s"]}}}), declared)
    assert bound.host == "mcp"


def test_an_app_for_a_component_the_project_lacks_is_refused():
    declared = record(overview={**OVERVIEW, "components": ["platform"]})
    with pytest.raises(Failure, match="apps.acme-sales.overview: .*platform"):
        bound_services(
            config({"acme-sales": {"overview": {"groups": ["s"]}}}, components=["mcp"]), declared
        )


def test_a_snowflake_app_renders_no_service():
    assert bound_services(config({"acme-sales": {"finance": {"groups": ["f"]}}}), record()) == []


@pytest.mark.parametrize("key", [{"hostname": "fin"}, {"local": False}])
def test_a_snowflake_app_refuses_host_choices(key):
    with pytest.raises(Failure, match=f"apps.acme-sales.finance.{next(iter(key))}: only a"):
        check_bindings(config({"acme-sales": {"finance": {"groups": ["f"], **key}}}), record())


def test_a_binding_for_an_undeclared_app_is_refused():
    with pytest.raises(Failure) as caught:
        check_bindings(config({"acme-sales": {"board": {"groups": ["s"]}}}), record())
    assert caught.value.code == ExitCode.CONFIG
    assert "apps.acme-sales.board: acme-sales declares no app board" in caught.value.message


def test_a_binding_for_an_extension_not_locked_is_refused():
    with pytest.raises(Failure, match="apps.acme-sales: .*vecten lock"):
        check_bindings(config({"acme-sales": {"overview": {"groups": ["s"]}}}), {})


def test_an_app_whose_default_hostname_is_reserved_names_the_key():
    declared = {"www": parse_app("acme-sales", "www", OVERVIEW).app}
    with pytest.raises(Failure, match="apps.acme-sales.www.hostname: www is reserved"):
        check_bindings(
            config({"acme-sales": {"www": {"groups": ["s"]}}}),
            {"acme-sales": Extension("1.0", (), apps=declared)},
        )


def test_a_default_hostname_taken_by_a_service_is_refused():
    rec = record()
    rec["acme-other"] = Extension(
        "1.0", (), services={"portal": parse_service("acme-other", "portal", PORTAL).service}
    )
    services = {
        "acme-other": {
            "portal": {
                "hostname": "overview",
                "routes": {"home": {"access": "login", "groups": ["s"]}},
            }
        }
    }
    with pytest.raises(Failure, match="hostname overview is bound twice"):
        check_bindings(
            config({"acme-sales": {"overview": {"groups": ["s"]}}}, services=services), rec
        )


def test_an_apps_service_clashing_with_a_bound_service_is_refused():
    rec = record()
    portal = parse_service("acme-sales", "app-overview", PORTAL).service
    rec["acme-sales"] = Extension(
        "1.0", (), services={"app-overview": portal}, apps=rec["acme-sales"].apps
    )
    services = {
        "acme-sales": {
            "app-overview": {
                "hostname": "portal",
                "routes": {"home": {"access": "login", "groups": ["s"]}},
            }
        }
    }
    with pytest.raises(Failure, match="svc-acme-sales-app-overview"):
        bound_services(
            config({"acme-sales": {"overview": {"groups": ["s"]}}}, services=services), rec
        )


@pytest.mark.parametrize("name", ["APP_OVERVIEW_CLIENT_ID", "MCP_APP_OVERVIEW_CLIENT_SECRET"])
def test_a_connector_may_not_declare_an_app_credential(name):
    with pytest.raises(ValidationError, match="analytics app"):
        config({}, connectors=[{"name": "hubspot", "source": "custom", "secrets": [name]}])


@pytest.mark.parametrize(
    ("first", "second", "principal"),
    [
        (("acme-sales", "a-b"), ("acme-sales", "a--b"), "svc_app_a_b"),
        (("acme-sales", "overview"), ("acme-other", "overview-"), "svc_app_overview"),
    ],
)
def test_two_apps_whose_principal_names_collide_are_refused(first, second, principal):
    apps = {}
    for (extension, name), label in ((first, "a"), (second, "b")):
        apps.setdefault(extension, {})[name] = {"groups": ["sales"], "hostname": label}
    keys = sorted(f"{e}.{n}" for e, n in (first, second))
    with pytest.raises(ValidationError) as refused:
        config(apps)
    message = str(refused.value)
    assert principal in message and all(key in message for key in keys)


def test_an_apps_principal_name_folds_every_run_of_other_characters():
    from vecten_shared.apps import principal_name

    assert principal_name("a--b") == principal_name("a-b") == "svc_app_a_b"
    assert principal_name("overview-") == "svc_app_overview"


def _saved_and_loaded(tmp_path, apps, existing):
    from vecten_shared.config import load_config, save_config

    if existing:
        (tmp_path / "vecten.toml").write_text(
            '[project]\nname = "demo"\nscaffold_version = "0.1.0"\n'
        )
    save_config(tmp_path, config(apps))
    return load_config(tmp_path)


@pytest.mark.parametrize("existing", [False, True], ids=["new file", "existing file"])
def test_an_accepted_snowflake_binding_is_accepted_after_a_save(tmp_path, existing):
    apps = {"acme-sales": {"finance": {"groups": ["finance"]}}}
    check_bindings(config(apps), record())
    loaded = _saved_and_loaded(tmp_path, apps, existing)
    check_bindings(loaded, record())
    text = (tmp_path / "vecten.toml").read_text()
    assert "local" not in text and "hostname" not in text


@pytest.mark.parametrize("existing", [False, True], ids=["new file", "existing file"])
@pytest.mark.parametrize(
    "chosen", [{}, {"local": True}, {"local": False}, {"hostname": "board", "local": False}]
)
def test_a_container_binding_keeps_its_host_choices_through_a_save(tmp_path, chosen, existing):
    apps = {"acme-sales": {"overview": {"groups": ["sales"], **chosen}}}
    loaded = _saved_and_loaded(tmp_path, apps, existing)
    binding = loaded.apps["acme-sales"]["overview"]
    assert binding.model_dump(exclude_none=True) == {"groups": ["sales"], **chosen}
    [bound] = bound_services(loaded, record())
    assert bound.binding.local is chosen.get("local", True)
    assert bound.binding.hostname == chosen.get("hostname", "overview")
