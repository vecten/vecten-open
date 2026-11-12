"""What extensions and projects may declare: secrets, a manifest, and settings per environment."""

import datetime

import pytest
import tomli_w

from vecten_shared import declarations as d
from vecten_shared.config import VectenConfig, load_config
from vecten_shared.extension_record import Extension
from vecten_shared.output import ExitCode, Failure

PROJECT = {"users": 1, "project": {"name": "a", "scaffold_version": "0.1.0"}}
SCHEMA = {
    "properties": {
        "daily_cap": {"type": "integer", "default": 1000, "minimum": 1},
        "dry_run": {"type": "boolean", "default": False},
        "region": {"type": "string", "enum": ["eu", "us"]},
    },
    "required": ["region"],
}


def record(settings=SCHEMA, secrets=("ACME_API_KEY", "MCP_ACME_TOKEN")):
    return {"acme": Extension("1.0", (), secrets=tuple(secrets), settings=settings)}


# --- secrets ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["ACME_API_KEY", "MCP_ACME_TOKEN", "SLACK_TOKEN"])
def test_ordinary_secret_names_are_accepted(name):
    assert d.secret_problem(name) is None


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("DEPLOY_ACME_KEY", "deploy"),
        ("ADMIN_ACME_KEY", "admin"),
        ("SETUP_ACME_KEY", "setup"),
        ("LOADER_PASSWORD", "stack"),
        ("MCP_SIGNING_KEY", "stack"),
        ("PACKAGE_INDEX_URL", "stack"),
        ("AIRFLOW_ACME", "stack"),
        ("HAS-DASH", "name"),
    ],
)
def test_reserved_and_malformed_secret_names_are_refused(name, said):
    assert said in d.secret_problem(name)


def test_a_secret_goes_to_the_component_its_prefix_names():
    assert d.component_for("MCP_ACME_TOKEN") == "mcp"
    assert d.component_for("ACME_API_KEY") == "platform"


# --- manifests -------------------------------------------------------------


def test_a_manifest_with_secrets_and_settings_reads_back():
    manifest = d.parse_manifest("acme", {"secrets": ["ACME_API_KEY"], "settings": SCHEMA})
    assert manifest.secrets == ("ACME_API_KEY",)
    assert manifest.settings == SCHEMA
    assert manifest.unknown == ()


def test_a_factory_manifest_is_called():
    assert d.parse_manifest("acme", lambda: {"secrets": []}).secrets == ()


def test_an_unknown_top_level_key_is_reported_not_refused():
    assert d.parse_manifest("acme", {"permissions": ["x"]}).unknown == ("permissions",)


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ({"secrets": "ACME_API_KEY"}, "secrets"),
        ({"secrets": ["DEPLOY_X"]}, "secrets"),
        ({"settings": {"properties": {"x": {"type": "date"}}}}, "settings"),
        ({"settings": {"properties": {"env": {"type": "string"}}}}, "settings"),
        ({"settings": {"properties": {"x": {"type": "string", "pattern": "a"}}}}, "settings"),
        ({"settings": {"properties": {}, "additionalProperties": True}}, "settings"),
        ({"settings": {"properties": {}, "required": ["missing"]}}, "settings"),
        ("not a mapping", "manifest"),
    ],
)
def test_a_malformed_known_key_is_refused_naming_extension_and_key(value, key):
    with pytest.raises(Failure) as caught:
        d.parse_manifest("acme", value)
    assert caught.value.code == ExitCode.CONFIG
    assert "acme" in caught.value.message and key in caught.value.message


# --- settings in vecten.toml -------------------------------------------------


def _load(tmp_path, **sections):
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps({**PROJECT, **sections}))
    return load_config(tmp_path)


def test_settings_for_a_listed_extension_and_the_project_load(tmp_path):
    config = _load(
        tmp_path,
        extensions=["acme"],
        settings={"acme": {"daily_cap": 5, "env": {"local": {"daily_cap": 1}}}, "project": {}},
    )
    assert config.settings["acme"]["daily_cap"] == 5


@pytest.mark.parametrize(
    ("settings", "said"),
    [
        ({"acme-other": {"x": 1}}, "settings.acme-other"),
        ({"acme": {"env": {"Bad Env": {}}}}, "Bad Env"),
        ({"acme": {"env": "prod"}}, "env"),
        ({"acme": {"day": datetime.date(2026, 1, 1)}}, "date"),
        ({"acme": {"env": {"prod": {"at": datetime.time(1, 2)}}}}, "date"),
    ],
)
def test_settings_namespaces_environments_and_values_are_checked_on_load(tmp_path, settings, said):
    with pytest.raises(Failure) as caught:
        _load(tmp_path, extensions=["acme"], settings=settings)
    assert caught.value.code == ExitCode.CONFIG
    assert said in caught.value.message


def test_an_extension_named_project_is_refused(tmp_path):
    with pytest.raises(Failure) as caught:
        _load(tmp_path, extensions=["project"])
    assert "project" in caught.value.message


def test_project_secrets_follow_the_secret_rules(tmp_path):
    assert _load(tmp_path, secrets=["SLACK_TOKEN"]).secrets == ["SLACK_TOKEN"]
    with pytest.raises(Failure, match="deploy"):
        _load(tmp_path, secrets=["DEPLOY_SLACK"])


# --- effective settings ----------------------------------------------------


def config(**extra):
    return VectenConfig.model_validate({**PROJECT, "extensions": ["acme"], **extra})


def test_an_override_replaces_the_namespace_value_which_replaces_the_default():
    c = config(
        settings={"acme": {"region": "eu", "dry_run": False, "env": {"staging": {"dry_run": True}}}}
    )
    assert d.effective_settings(c, record(), "acme", "staging") == {
        "daily_cap": 1000,
        "dry_run": True,
        "region": "eu",
    }
    assert d.effective_settings(c, record(), "acme", "prod")["dry_run"] is False


def test_an_override_replaces_a_whole_value_with_no_deep_merge():
    schema = {"properties": {"limits": {"type": "object", "default": {"a": 1, "b": 2}}}}
    c = config(settings={"acme": {"env": {"prod": {"limits": {"a": 9}}}}})
    assert d.effective_settings(c, record(schema), "acme", "prod") == {"limits": {"a": 9}}


@pytest.mark.parametrize(
    ("settings", "env", "said"),
    [
        ({"acme": {"region": "eu", "dialy_cap": 5}}, "local", "settings.acme.dialy_cap"),
        ({"acme": {"region": "eu", "env": {"prod": {"daily_cap": "500"}}}}, "prod", "prod"),
        ({"acme": {}}, "local", "settings.acme.region"),
        ({"acme": {"region": "asia"}}, "local", "settings.acme.region"),
        ({"acme": {"region": "eu", "daily_cap": 0}}, "local", "settings.acme.daily_cap"),
    ],
)
def test_check_settings_names_the_key_and_the_environment(settings, env, said):
    with pytest.raises(Failure) as caught:
        d.check_settings(config(settings=settings), record(), env)
    assert caught.value.code == ExitCode.CONFIG
    assert said in caught.value.message and env in caught.value.message


def test_settings_for_an_extension_without_a_schema_are_refused():
    c = config(settings={"acme": {"x": 1}})
    with pytest.raises(Failure, match="settings.acme"):
        d.check_settings(c, record(settings=None), "local")


def test_project_settings_need_only_json_values():
    c = config(settings={"project": {"channel": "#data", "env": {"prod": {"channel": "#ops"}}}})
    d.check_settings(c, record(settings={"properties": {}}), "prod")
    assert d.effective_settings(c, record(), "project", "prod") == {"channel": "#ops"}


def test_environments_to_check_are_local_and_every_named_override():
    c = config(
        settings={
            "acme": {"env": {"staging": {}, "prod": {}}},
            "project": {"env": {"dev": {}}},
        }
    )
    assert d.environments_named(c) == ["dev", "local", "prod", "staging"]


# --- review fixes ----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "AWS_ACCESS_KEY_ID",
        "AWS_REGION",
        "SCW_SECRET_KEY",
        "VECTEN_ARTIFACT_URL",
        "VECTEN_SCW_ACCESS_KEY",
        "PATH",
        "PYTHONPATH",
        "WAREHOUSE_ADMIN_PASSWORD",
        "WAREHOUSE_ADMIN_PRIVATE_KEY",
        "MCP_PROVISIONER_PRIVATE_KEY",
        "SNOWFLAKE_LOADER_PRIVATE_KEY",
    ],
)
def test_names_the_stack_or_its_hosts_use_cannot_be_declared(name):
    assert d.secret_problem(name) is not None


@pytest.mark.parametrize(
    "settings",
    [
        {"properties": {"cap": {"type": "integer", "minimum": "5"}}},
        {"properties": {"cap": {"type": "string", "enum": "ab"}}},
        {"properties": {"cap": {"type": "integer"}}, "required": [["cap"]]},
        {"properties": {"cap": {"type": "array", "items": {"$ref": "https://evil.example/x"}}}},
        {"properties": {"cap": {"type": "integer", "default": "many"}}},
        {"properties": {"cap": {"type": "number", "default": float("inf")}}},
        {"properties": {1: {"type": "integer"}}},
    ],
)
def test_a_settings_schema_that_is_not_valid_json_schema_is_refused_at_lock(settings):
    with pytest.raises(Failure) as caught:
        d.parse_manifest("acme", {"settings": settings})
    assert caught.value.code == ExitCode.CONFIG
    assert "acme" in caught.value.message and "settings" in caught.value.message


def test_array_items_use_the_same_keyword_subset():
    schema = {"properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
    assert d.parse_manifest("acme", {"settings": schema}).settings == schema


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_setting_with_no_json_number_is_refused_on_load(tmp_path, value):
    with pytest.raises(Failure, match="settings.acme"):
        _load(tmp_path, extensions=["acme"], settings={"acme": {"cap": value}})


def test_settings_without_a_record_entry_name_vecten_lock():
    c = config(settings={"acme": {"daily_cap": 5}})
    with pytest.raises(Failure, match="vecten lock"):
        d.check_settings(c, {}, "local")
