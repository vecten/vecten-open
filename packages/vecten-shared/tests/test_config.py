import tomllib

import pytest
import tomli_w
from pydantic import ValidationError

from vecten_shared.config import (
    ConnectorDecl,
    VectenConfig,
    load_config,
    parse_warehouse_locator,
    save_config,
)
from vecten_shared.output import ExitCode, Failure

MINIMAL = """
users = 10
size = "small"
components = ["platform", "mcp"]
profile = "standard"
wanted = ["hubspot"]
extensions = []

[project]
name = "acme"
scaffold_version = "0.1.0"
convention_version = 1

[warehouse]
kind = "postgres"
existing = false

[mcp]
admins = []
"""


def test_round_trip(tmp_path):
    (tmp_path / "vecten.toml").write_text(MINIMAL)
    cfg = load_config(tmp_path)
    assert cfg.components == ["platform", "mcp"] and cfg.wanted == ["hubspot"]
    cfg.environments["prod"].bootstrapped = True
    save_config(tmp_path, cfg)
    again = load_config(tmp_path)
    assert again.environments["prod"].bootstrapped is True
    assert again.deploy is None


def test_malformed_toml_names_file_and_error(tmp_path):
    (tmp_path / "vecten.toml").write_text("users = \n")
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG
    assert "vecten.toml" in exc.value.message and "line 1" in exc.value.message


def test_unknown_component_is_a_config_error(tmp_path):
    (tmp_path / "vecten.toml").write_text(MINIMAL.replace('"mcp"', '"bigquery"'))
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG and "components" in exc.value.message


def test_default_environment_facts_are_false():
    cfg = VectenConfig.model_validate(
        {
            "users": 3,
            "components": ["mcp"],
            "project": {"name": "x", "scaffold_version": "0.1.0"},
            "warehouse": {"kind": "postgres"},
        }
    )
    facts = cfg.environments["prod"]
    assert (
        facts.bootstrapped is False
        and facts.deployed_commit is None
        and facts.first_user_added is False
    )


@pytest.mark.parametrize(
    "url,kind,host,port,database,account",
    [
        (
            "postgres://dw.internal:5432/analytics",
            "postgres",
            "dw.internal",
            5432,
            "analytics",
            None,
        ),
        ("postgresql://dw.internal/analytics", "postgres", "dw.internal", 5432, "analytics", None),
        (
            "snowflake://ab12345.eu-central-1/analytics",
            "snowflake",
            None,
            None,
            "analytics",
            "ab12345.eu-central-1",
        ),
    ],
)
def test_locator_parsing(url, kind, host, port, database, account):
    w = parse_warehouse_locator(url)
    assert (w.kind, w.host, w.port, w.database, w.account) == (kind, host, port, database, account)
    assert w.existing is True


@pytest.mark.parametrize(
    "url",
    [
        "postgres://admin:secret@host/dw",
        "postgres://host/dw?password=secret",
        "snowflake://user:pw@ab12345/dw",
    ],
)
def test_warehouse_url_password_forms_refused(url):
    with pytest.raises(Failure) as exc:
        parse_warehouse_locator(url)
    assert exc.value.code == ExitCode.USAGE
    assert "secret set WAREHOUSE_ADMIN_PASSWORD" in exc.value.message


def test_unsupported_locator_scheme():
    with pytest.raises(Failure) as exc:
        parse_warehouse_locator("bigquery://proj/ds")
    assert "postgres" in exc.value.message and "snowflake" in exc.value.message


@pytest.mark.parametrize(
    "url,fragment",
    [
        ("postgres://h:abc/db", "port"),
        ("postgres://h:99999/db", "port"),
        ("postgres:///db", "host"),
        ("snowflake:///db", "account"),
    ],
)
def test_malformed_locators_are_usage_errors(url, fragment):
    with pytest.raises(Failure) as exc:
        parse_warehouse_locator(url)
    assert exc.value.code == ExitCode.USAGE and fragment in exc.value.message


def test_unsupported_scheme_names_every_scheme_and_driver_suffix():
    with pytest.raises(Failure) as exc:
        parse_warehouse_locator("postgresql+psycopg2://h/db")
    assert exc.value.code == ExitCode.USAGE
    assert "postgresql://" in exc.value.message and "+psycopg2" in exc.value.message


@pytest.mark.parametrize(
    "url,user",
    [
        ("postgresql://mcp_admin@warehouse.acme.test/analytics", "mcp_admin"),
        ("postgres://dw%40admin@host/dw", "dw@admin"),
        ("snowflake://VECTEN_ADMIN@acct/db", "VECTEN_ADMIN"),
        ("postgres://host/dw", None),
    ],
)
def test_the_locator_keeps_the_admin_user_it_names(url, user):
    assert parse_warehouse_locator(url).admin_user == user


def test_snowflake_database_is_first_path_segment():
    w = parse_warehouse_locator("snowflake://acct/db/schema?warehouse=wh")
    assert (w.account, w.database) == ("acct", "db")


def test_missing_config_is_refused_naming_init(tmp_path):
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.REFUSED and "vecten init" in exc.value.message


def test_undecodable_config_is_a_config_error(tmp_path):
    (tmp_path / "vecten.toml").write_bytes(b"users = \xff\n")
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG and "vecten.toml" in exc.value.message


def test_has_component(tmp_path):
    (tmp_path / "vecten.toml").write_text(MINIMAL.replace('["platform", "mcp"]', '["mcp"]'))
    cfg = load_config(tmp_path)
    assert cfg.has("mcp") and not cfg.has("platform")


FULL = (
    MINIMAL
    + """
[deploy]
cloud = "aws"
region = "eu-central-1"
domain = "data.acme.com"
dns_zone = "acme.com"
github_repository = "acme/platform"

[[connectors]]
name = "hubspot"
source = "dlt"
dependencies = ["requests"]

[connectors.access]
groups = ["sales"]

[[connectors]]
name = "stripe"
source = "custom"

[environments.prod]
bootstrapped = true
deployed_commit = "abc1234"
connector_access_applied = ["hubspot"]
"""
)


def test_full_round_trip(tmp_path):
    (tmp_path / "vecten.toml").write_text(FULL)
    cfg = load_config(tmp_path)
    assert cfg.connectors[0].access.groups == ["sales"] and cfg.connectors[1].access is None
    assert cfg.environments["prod"].deployed_commit == "abc1234"
    save_config(tmp_path, cfg)
    assert load_config(tmp_path) == cfg
    assert [p.name for p in tmp_path.iterdir()] == ["vecten.toml"]


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("region", "eu-central-1;x"),
        ("domain", "data.acme.com (our subdomain)"),
        ("dns_zone", "acme"),
        ("github_repository", "-acme/platform"),
    ],
)
def test_deploy_section_refuses_values_outside_their_rule(tmp_path, key, bad):
    good = {
        "region": "eu-central-1",
        "domain": "data.acme.com",
        "dns_zone": "acme.com",
        "github_repository": "acme/platform",
    }
    text = FULL.replace(f'{key} = "{good[key]}"', f'{key} = "{bad}"')
    assert bad in text
    (tmp_path / "vecten.toml").write_text(text)
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG and f"deploy.{key}" in exc.value.message


def _config(**connectors):
    return VectenConfig.model_validate(
        {
            "users": 3,
            "project": {"name": "acme", "scaffold_version": "0.1.0"},
            "connectors": list(connectors.values()),
        }
    )


def test_connector_declaration_defaults():
    decl = ConnectorDecl(name="hubspot", source="dlt")
    assert decl.groups == {"default": []}
    assert decl.secrets == [] and decl.dependencies == []
    assert decl.extension is None and decl.version is None and decl.scaffold_hash is None
    assert decl.access is None


def test_connector_source_kind_is_closed():
    with pytest.raises(ValidationError):
        ConnectorDecl(name="x", source="kafka")


def test_config_connector_lookup_by_name():
    cfg = _config(a={"name": "hubspot", "source": "dlt"})
    assert cfg.connector("hubspot").source == "dlt"
    assert cfg.connector("stripe") is None


def test_connector_declaration_round_trips_through_toml(tmp_path):
    cfg = _config(
        a={
            "name": "hubspot",
            "source": "dlt",
            "secrets": ["HUBSPOT_API_KEY"],
            "groups": {"default": [], "crm": ["companies", "deals"]},
            "access": {"groups": ["sales"]},
            "dependencies": ["requests-oauthlib>=1.3,<2"],
            "scaffold_hash": "abc",
        }
    )
    save_config(tmp_path, cfg)
    text = (tmp_path / "vecten.toml").read_text()
    assert "scaffold_hash" in text
    loaded = load_config(tmp_path)
    assert loaded.connector("hubspot") == cfg.connector("hubspot")
    raw = tomllib.loads(text)
    assert raw["connectors"][0]["groups"]["crm"] == ["companies", "deals"]


@pytest.mark.parametrize(
    "connector,field",
    [
        ({"name": "Hub-Spot", "source": "dlt"}, "name"),
        ({"name": "hubspot", "source": "dlt", "secrets": ["HUBSPOT-KEY"]}, "secrets"),
        ({"name": "hubspot", "source": "dlt", "access": {"groups": ["sales-ops"]}}, "access"),
        ({"name": "hubspot", "source": "dlt", "access": {"groups": []}}, "access"),
        ({"name": "hubspot", "source": "dlt", "groups": {"../x": []}}, "groups"),
    ],
    ids=["connector-name", "secret", "access-group", "empty-access", "dag-group"],
)
def test_hand_edited_connector_names_are_config_errors(tmp_path, connector, field):
    save_config(tmp_path, _config(a={"name": "ok", "source": "dlt"}))
    raw = tomllib.loads((tmp_path / "vecten.toml").read_text())
    raw["connectors"] = [connector]
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps(raw))
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG
    assert f"connectors.0.{field}" in exc.value.message


@pytest.mark.parametrize("env", ["Prod", "local", "prod/../x"])
def test_environment_keys_follow_the_environment_rule(tmp_path, env):
    save_config(tmp_path, _config(a={"name": "ok", "source": "dlt"}))
    raw = tomllib.loads((tmp_path / "vecten.toml").read_text())
    raw["environments"] = {env: {"bootstrapped": True}}
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps(raw))
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG and "environments" in exc.value.message


def test_mcp_section_defaults_and_round_trip(tmp_path):
    cfg = VectenConfig.model_validate(
        {"users": 1, "project": {"name": "a", "scaffold_version": "0.1.0"}}
    )
    assert cfg.mcp.auth == "oidc" and cfg.mcp.oidc_issuer is None and cfg.mcp.admins == []
    cfg.mcp.auth = "password"
    cfg.mcp.oidc_issuer = "https://accounts.example.com"
    cfg.environments["prod"].alert_webhook_asked = True
    save_config(tmp_path, cfg)
    loaded = load_config(tmp_path)
    assert loaded.mcp.auth == "password"
    assert loaded.mcp.oidc_issuer == "https://accounts.example.com"
    assert loaded.environments["prod"].alert_webhook_asked is True


def test_mcp_auth_refuses_invalid_plugin_names():
    with pytest.raises(ValidationError):
        VectenConfig.model_validate(
            {
                "users": 1,
                "project": {"name": "a", "scaffold_version": "0.1.0"},
                "mcp": {"auth": "../ldap"},
            }
        )


def test_cloud_ids_default_empty_and_round_trip(tmp_path):
    cfg = VectenConfig.model_validate(
        {"users": 1, "project": {"name": "a", "scaffold_version": "0.1.0"}}
    )
    assert cfg.environments["prod"].cloud_ids == {}
    cfg.environments["prod"].cloud_ids = {
        "project": "11111111-2222-3333-4444-555555555555",
        "state_project": "66666666-7777-8888-9999-000000000000",
    }
    save_config(tmp_path, cfg)
    loaded = load_config(tmp_path)
    assert loaded.environments["prod"].cloud_ids == {
        "project": "11111111-2222-3333-4444-555555555555",
        "state_project": "66666666-7777-8888-9999-000000000000",
    }


@pytest.mark.parametrize(
    "cloud_ids",
    [{"project": "a b"}, {"project": '"; rm -rf /'}, {"Project": "abc"}, {"project": ""}],
)
def test_cloud_ids_hold_plain_identifiers_only(tmp_path, cloud_ids):
    raw = {
        "users": 1,
        "project": {"name": "a", "scaffold_version": "0.1.0"},
        "environments": {"prod": {"cloud_ids": cloud_ids}},
    }
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps(raw))
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG and "cloud_ids" in exc.value.message


def test_cloud_ids_expiries_are_iso_timestamps(tmp_path):
    raw = {
        "users": 1,
        "project": {"name": "a", "scaffold_version": "0.1.0"},
        "environments": {
            "prod": {
                "cloud_ids": {"key_deploy": "SCW0123", "expires_deploy": "2027-10-18T12:00:00Z"}
            }
        },
    }
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps(raw))
    facts = load_config(tmp_path).environments["prod"]
    assert facts.cloud_ids["expires_deploy"] == "2027-10-18T12:00:00Z"


@pytest.mark.parametrize(
    "cloud_ids",
    [
        {"expires_deploy": "soon"},
        {"expires_deploy": "2027-13-40T12:00:00Z"},
        {"expires_deploy": "2027-10-18T12:00:00"},
        {"project": "2027-10-18T12:00:00Z"},
    ],
)
def test_cloud_ids_expiries_refuse_anything_else(tmp_path, cloud_ids):
    raw = {
        "users": 1,
        "project": {"name": "a", "scaffold_version": "0.1.0"},
        "environments": {"prod": {"cloud_ids": cloud_ids}},
    }
    (tmp_path / "vecten.toml").write_text(tomli_w.dumps(raw))
    with pytest.raises(Failure) as exc:
        load_config(tmp_path)
    assert exc.value.code == ExitCode.CONFIG and "cloud_ids" in exc.value.message
