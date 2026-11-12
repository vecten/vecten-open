import json
from types import SimpleNamespace

import pytest

from vecten.mcp_onboarding import catalog_rows
from vecten_shared.config import parse_warehouse_locator
from vecten_shared.identity_setup import introspection_document, provisioner_script
from vecten_shared.keypair import load_private_key, public_body
from vecten_shared.osi.validate import validate_osi
from vecten_shared.output import ExitCode, Failure


def test_postgres_script_has_exact_roles_and_no_embedded_passwords():
    sql = provisioner_script("postgres", database="warehouse", issuer="https://mcp.example")
    assert "provisioner LOGIN CREATEROLE" in sql
    assert "impersonator LOGIN NOINHERIT" in sql
    assert "ADMIN TRUE, INHERIT FALSE, SET FALSE" in sql
    assert "CREATE DATABASE" not in sql and "LOADER_PASSWORD" not in sql
    assert "\\getenv" in sql


def names(database, warehouse="W"):
    """The fixed layout's names, over a test database and warehouse in `org-account`."""
    from dataclasses import replace

    from vecten_shared.config import VectenConfig
    from vecten_shared.provider import snowflake_names

    config = VectenConfig.model_validate(
        {
            "users": 1,
            "project": {"name": "acme", "scaffold_version": "0.1.1"},
            "warehouse": {"kind": "snowflake", "account": "org-account"},
        }
    )
    return replace(snowflake_names(config, "prod"), database=database, warehouse=warehouse)


def test_snowflake_script_names_custom_oauth_login_mapping():
    sql = provisioner_script("snowflake", issuer="https://mcp.example", names=names("WAREHOUSE"))
    assert "EXTERNAL_OAUTH_TYPE = CUSTOM" in sql
    assert "https://mcp.example/jwks" in sql
    assert "LOGIN_NAME" in sql and "('sub')" in sql
    assert "org-account.snowflakecomputing.com" in sql


def test_information_schema_draft_validates(tmp_path):
    rows = [
        ("warehouse", "marts", "orders", "order_id", "integer"),
        ("warehouse", "marts", "orders", "created_at", "timestamp without time zone"),
        ("warehouse", "marts", "orders", "amount", "numeric"),
    ]
    doc = introspection_document(rows, "postgres")
    (tmp_path / "datasets.json").write_text(json.dumps(doc))
    result = validate_osi(tmp_path, warehouse_kind="postgres")
    assert result.ok, result.errors
    dataset = doc["semantic_model"][0]["datasets"][0]
    assert all(d["description"] == "" for d in doc["semantic_model"][0]["datasets"])
    assert dataset["source"] == "warehouse.marts.orders"
    assert dataset["fields"][1]["dimension"]["is_time"]


def test_introspection_command_writes_draft_and_refuses_overwrite(invoke, monkeypatch, workdir):
    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    from vecten_shared.config import load_config, save_config

    config = load_config(workdir)
    config.components = ["mcp"]
    config.warehouse.existing = True
    config.warehouse.host = "warehouse.example"
    config.warehouse.database = "warehouse"
    save_config(workdir, config)
    (workdir / "osi" / "datasets.json").unlink(missing_ok=True)
    monkeypatch.setattr(
        "vecten.mcp_onboarding.catalog_rows",
        lambda *args: [("warehouse", "analytics", "orders", "id", "integer")],
    )
    result = invoke("mcp", "introspect", "--schema", "analytics", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["datasets"] == 1
    assert invoke("mcp", "introspect", "--schema", "analytics", "--json").exit_code != 0


def test_introspection_fills_the_empty_model_init_renders(invoke, monkeypatch, workdir):
    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    from vecten_shared.config import load_config, save_config

    config = load_config(workdir)
    config.components = ["mcp"]
    config.warehouse.existing = True
    config.warehouse.host = "warehouse.example"
    config.warehouse.database = "warehouse"
    save_config(workdir, config)
    assert (workdir / "osi" / "datasets.json").exists()
    monkeypatch.setattr(
        "vecten.mcp_onboarding.catalog_rows",
        lambda *args: [("warehouse", "analytics", "orders", "id", "integer")],
    )
    result = invoke("mcp", "introspect", "--schema", "analytics", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["datasets"] == 1


def _connect_recorder(monkeypatch, module, attribute):
    seen = {}

    def connect(**kwargs):
        seen.update(kwargs)
        raise OSError("no warehouse here")

    monkeypatch.setattr(module, attribute, connect)
    monkeypatch.setattr(
        "vecten.mcp_onboarding.LocalProvider",
        lambda *args, **kwargs: SimpleNamespace(get_secret=lambda name: "admin-password"),
    )
    return seen


@pytest.mark.parametrize("flag,user", [(None, "mcp_admin"), ("dba", "dba")])
def test_postgres_introspection_connects_as_the_locator_user_unless_told_otherwise(
    monkeypatch, tmp_path, flag, user
):
    import psycopg

    seen = _connect_recorder(monkeypatch, psycopg, "connect")
    locator = "postgresql://mcp_admin@warehouse.acme.test/analytics"
    config = SimpleNamespace(warehouse=parse_warehouse_locator(locator))
    with pytest.raises(Failure):
        catalog_rows(tmp_path, config, ["marts"], flag)
    assert seen["user"] == user


def _snowflake_connect(monkeypatch, *, refuse=None):
    """Records each Snowflake connect's arguments; answers an empty catalog, or raises `refuse`."""
    import snowflake.connector

    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            pass

        def fetchall(self):
            return []

    def connect(**kwargs):
        calls.append(kwargs)
        if refuse is not None:
            raise refuse
        return SimpleNamespace(cursor=Cursor, close=lambda: None)

    monkeypatch.setattr(snowflake.connector, "connect", connect)
    return calls


SNOWFLAKE_ADMIN = SimpleNamespace(
    warehouse=parse_warehouse_locator("snowflake://VECTEN_ADMIN@acct/db")
)


def _stored_admin_key(root):
    from vecten_shared.provider.local import LocalProvider

    return LocalProvider(root, inside_compose=False).get_secret("WAREHOUSE_ADMIN_PRIVATE_KEY")


def test_snowflake_introspection_takes_the_locator_user_without_a_flag(monkeypatch, tmp_path):
    calls = _snowflake_connect(monkeypatch)
    catalog_rows(tmp_path, SNOWFLAKE_ADMIN, ["MARTS"], None)
    assert calls[0]["user"] == "VECTEN_ADMIN"


def test_snowflake_introspection_signs_in_with_a_key_pair_it_generates(monkeypatch, tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa

    calls = _snowflake_connect(monkeypatch)
    catalog_rows(tmp_path, SNOWFLAKE_ADMIN, ["MARTS"], None)
    [kwargs] = calls
    # Snowflake offers a service user no password, and a person's password meets MFA.
    assert "password" not in kwargs
    assert isinstance(kwargs["private_key"], rsa.RSAPrivateKey)
    stored = _stored_admin_key(tmp_path)
    # The local .env holds no newline, so the key is kept as its one-line body.
    assert "\n" not in stored
    signed_with = kwargs["private_key"].public_key().public_numbers()
    assert signed_with == load_private_key(stored, "K").public_key().public_numbers()


def test_a_second_snowflake_introspection_reuses_the_stored_key(monkeypatch, tmp_path):
    _snowflake_connect(monkeypatch)
    catalog_rows(tmp_path, SNOWFLAKE_ADMIN, ["MARTS"], None)
    first = _stored_admin_key(tmp_path)
    catalog_rows(tmp_path, SNOWFLAKE_ADMIN, ["MARTS"], None)
    assert _stored_admin_key(tmp_path) == first


def test_a_key_snowflake_does_not_know_names_the_statement_that_registers_it(monkeypatch, tmp_path):
    from snowflake.connector.errors import DatabaseError

    refusal = DatabaseError(msg="JWT token is invalid.", errno=390144)
    _snowflake_connect(monkeypatch, refuse=refusal)
    with pytest.raises(Failure) as caught:
        catalog_rows(tmp_path, SNOWFLAKE_ADMIN, ["MARTS"], None)
    assert caught.value.code == ExitCode.PREREQUISITE
    body = public_body(load_private_key(_stored_admin_key(tmp_path), "K"))
    assert f"ALTER USER \"VECTEN_ADMIN\" SET RSA_PUBLIC_KEY = '{body}';" in caught.value.message
    assert _stored_admin_key(tmp_path) not in caught.value.message


def test_other_snowflake_failures_stay_tool_failures(monkeypatch, tmp_path):
    from snowflake.connector.errors import DatabaseError

    _snowflake_connect(monkeypatch, refuse=DatabaseError(msg="no warehouse", errno=606))
    with pytest.raises(Failure) as caught:
        catalog_rows(tmp_path, SNOWFLAKE_ADMIN, ["MARTS"], None)
    assert caught.value.code == ExitCode.TOOL_FAILED


def test_script_printing_does_not_claim_administrator_ran_it(invoke, workdir):
    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    from vecten_shared.config import load_config, save_config
    from vecten_shared.records import read_record

    config = load_config(workdir)
    config.components = ["mcp"]
    config.warehouse.existing = True
    config.warehouse.database = "warehouse"
    save_config(workdir, config)
    result = invoke("mcp", "provisioner-script", "--json")
    assert result.exit_code == 0, result.output
    assert not read_record(workdir, "provisioner-local")
    assert invoke("mcp", "provisioner-script", "--confirm-run", "--json").exit_code == 0
    assert read_record(workdir, "provisioner-local")["confirmed"]


def test_postgres_script_opens_with_its_psql_and_environment_requirements():
    first = provisioner_script("postgres", database="w", issuer="https://mcp.example").splitlines()[
        0
    ]
    assert first.startswith("-- ")
    assert "psql 15" in first
    assert "MCP_PROVISIONER_PASSWORD" in first and "MCP_IMPERSONATOR_PASSWORD" in first


def test_plain_provisioner_script_output_is_sql_and_comments_only(invoke, workdir):
    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    from vecten_shared.config import load_config, save_config

    config = load_config(workdir)
    config.components = ["mcp"]
    config.warehouse.existing = True
    config.warehouse.database = "warehouse"
    save_config(workdir, config)
    result = invoke("mcp", "provisioner-script")
    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    assert not [line for line in lines if line.startswith("vecten ")]
    assert "-- then: vecten secret set MCP_PROVISIONER_PASSWORD" in result.output
    # The administrator's run comes first: the record is what clears the `next` item.
    then = [line for line in lines if line.startswith("-- then: ")]
    assert then[0] == "-- then: vecten mcp provisioner-script --confirm-run"
    confirmed = invoke("mcp", "provisioner-script", "--confirm-run")
    assert confirmed.exit_code == 0, confirmed.output
    assert "vecten mcp provisioner-script" not in confirmed.output
    # The confirmation is a record, not a second run of the script.
    assert "CREATE ROLE" not in confirmed.output and "--" not in confirmed.output
    assert "recorded that the administrator ran the provisioner script for local" in (
        confirmed.output
    )
    assert "then: vecten secret set MCP_PROVISIONER_PASSWORD" in confirmed.output
    data = json.loads(invoke("mcp", "provisioner-script", "--confirm-run", "--json").stdout)
    assert "sql" not in json.dumps(data)


def test_snowflake_script_opens_with_the_privilege_its_future_grants_need():
    # In a regular schema a future grant needs MANAGE GRANTS, even from the schema's owner.
    first = provisioner_script(
        "snowflake", issuer="https://mcp.example", names=names("D")
    ).splitlines()[0]
    assert first.startswith("-- ")
    assert "ACCOUNTADMIN" in first and "MANAGE GRANTS" in first


def test_snowflake_script_ends_with_the_leftover_drops():
    sql = provisioner_script(
        "snowflake", issuer="https://mcp.example", names=names("ANALYTICS", "ANALYTICS_WH")
    )
    statements = [line for line in sql.strip().splitlines() if not line.startswith("--")]
    assert statements[-3:] == [
        "SHOW GRANTS TO ROLE PROVISIONER;",
        "SELECT 'DROP ' || \"granted_on\" || ' \"' || \"name\" || '\";' AS leftover",
        "FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE \"privilege\" = 'OWNERSHIP' AND "
        "(\"granted_on\" = 'USER' OR (\"granted_on\" = 'ROLE' AND \"name\" <> 'ANALYST'));",
    ]
    comment = sql[sql.index("SHOW GRANTS TO ROLE PROVISIONER") - 400 :]
    assert "state" in comment and "snapshot" in comment


def test_snowflake_script_prints_revokes_for_grants_outside_its_database():
    # An account that hosted an earlier install leaves PROVISIONER grants the MCP refuses.
    sql = provisioner_script(
        "snowflake", issuer="https://mcp.example", names=names("ANALYTICS", "ANALYTICS_WH")
    )
    block = sql[sql.index("AS leftover_grant") - 400 : sql.index("AS leftover_grant") + 400]
    assert "'REVOKE ' || \"privilege\" || ' ON ' || \"granted_on\"" in block
    assert "FROM ROLE PROVISIONER;'" in block
    assert "'DROP SCHEMA IF EXISTS ' || SPLIT_PART(\"name\", '.', 1) || '.VECTEN_ACCESS;'" in block
    assert "NOT STARTSWITH(\"name\", 'ANALYTICS.')" in block
    assert "\"name\" <> 'ANALYTICS'" in block and "\"name\" <> 'ANALYTICS_WH'" in block
    assert "GRANT OWNERSHIP" in block and "REVOKE CURRENT GRANTS" in block
    assert sql.index("AS leftover_grant") < sql.index("AS leftover\n")


def test_snowflake_script_rerun_points_the_integration_at_this_deployment():
    # CREATE ... IF NOT EXISTS keeps an integration an earlier install left with other values.
    sql = provisioner_script("snowflake", issuer="https://mcp.example", names=names("D"))
    assert (
        "ALTER SECURITY INTEGRATION VECTEN_EXTERNAL_OAUTH SET\n"
        "  EXTERNAL_OAUTH_ISSUER = 'https://mcp.example'\n"
        "  EXTERNAL_OAUTH_JWS_KEYS_URL = 'https://mcp.example/jwks'\n"
        "  EXTERNAL_OAUTH_AUDIENCE_LIST = ('https://org-account.snowflakecomputing.com');" in sql
    )


def test_snowflake_script_seeds_scoped_data_access_without_owner_power():
    sql = provisioner_script(
        "snowflake", issuer="https://mcp.example", names=names("ANALYTICS", "ANALYTICS_WH")
    )
    assert 'GRANT USAGE ON DATABASE "ANALYTICS" TO ROLE ANALYST;' in sql
    assert 'GRANT USAGE ON WAREHOUSE "ANALYTICS_WH" TO ROLE ANALYST;' in sql
    for schema in ("CORE", "MARTS"):
        target = f'"ANALYTICS"."{schema}"'
        for role in ("ANALYST", "PROVISIONER"):
            assert f"GRANT USAGE ON SCHEMA {target} TO ROLE {role};" in sql
            suffix = " WITH GRANT OPTION" if role == "PROVISIONER" else ""
            for shape in ("ALL", "FUTURE"):
                assert (
                    f"GRANT SELECT ON {shape} TABLES IN SCHEMA {target} TO ROLE {role}{suffix};"
                    in sql
                )
    # The leftover note advises an administrator in a comment; no statement grants ownership.
    statements = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    assert "MANAGE GRANTS" not in statements
    assert "GRANT OWNERSHIP" not in statements
    assert "CREATE WAREHOUSE" not in statements


def test_snowflake_script_updates_secondary_role_mode_on_rerun():
    sql = provisioner_script("snowflake", issuer="https://mcp.example", names=names("D"))
    assert (
        "ALTER SECURITY INTEGRATION VECTEN_EXTERNAL_OAUTH "
        "SET EXTERNAL_OAUTH_ANY_ROLE_MODE = ENABLE;" in sql
    )


def _mcp_only_project(invoke, workdir):
    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    from vecten_shared.config import load_config, save_config

    config = load_config(workdir)
    config.components = ["mcp"]
    config.warehouse.existing = True
    config.warehouse.host = "warehouse.example"
    config.warehouse.database = "warehouse"
    save_config(workdir, config)
    (workdir / "osi" / "datasets.json").unlink(missing_ok=True)


def test_introspect_with_nothing_in_the_convention_names_the_schemas_it_sees(
    invoke, monkeypatch, workdir
):
    _mcp_only_project(invoke, workdir)
    monkeypatch.setattr("vecten.mcp_onboarding.catalog_rows", lambda *args: [])
    monkeypatch.setattr("vecten.mcp_onboarding.visible_schemas", lambda *args: ["sales"])
    result = invoke("mcp", "introspect", "--json")
    assert result.exit_code == ExitCode.PREREQUISITE, result.output
    error = json.loads(result.output)["error"]
    assert "sales" in error and "core" in error and "marts" in error
    assert "views in marts" in error


def test_introspect_outside_the_convention_warns(invoke, monkeypatch, workdir):
    _mcp_only_project(invoke, workdir)
    monkeypatch.setattr(
        "vecten.mcp_onboarding.catalog_rows",
        lambda *args: [("warehouse", "analytics", "orders", "id", "integer")],
    )
    result = invoke("mcp", "introspect", "--schema", "analytics", "--schema", "marts", "--json")
    assert result.exit_code == 0, result.output
    warnings = json.loads(result.output)["warnings"]
    assert len(warnings) == 1
    assert "analytics" in warnings[0] and "cannot read" in warnings[0]
    plain = invoke("mcp", "introspect", "--schema", "analytics", "--replace")
    assert plain.exit_code == 0, plain.output
    assert warnings[0] in plain.output


def test_snowflake_introspection_matches_unquoted_schema_names(monkeypatch, tmp_path):
    import snowflake.connector

    executed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append(params)

        def fetchall(self):
            return []

    connection = SimpleNamespace(cursor=Cursor, close=lambda: None)
    monkeypatch.setattr(snowflake.connector, "connect", lambda **kwargs: connection)
    config = SimpleNamespace(warehouse=parse_warehouse_locator("snowflake://VECTEN_ADMIN@acct/db"))
    catalog_rows(tmp_path, config, ["core", "marts"], None)
    # Snowflake stores an unquoted schema name in upper case.
    assert list(executed[0]) == ["CORE", "MARTS"]


class KeyStore:
    """The environment's secrets store as `secret set --env` reaches it."""

    values: dict[str, str] = {}
    written: list[str] = []

    def __init__(self, project_root, env):
        pass

    def get_secret(self, name):
        if name not in self.values:
            raise Failure(ExitCode.PREREQUISITE, f"secret {name} is not set in prod")
        return self.values[name]

    def put_secret(self, name, value):
        type(self).written.append(name)
        type(self).values[name] = value

    def list_secrets(self):
        return sorted(self.values)


@pytest.fixture
def platform_snowflake(tmp_path, monkeypatch):
    from vecten_shared import provider as provider_module

    from .fixture_project import render_fixture

    monkeypatch.setattr(KeyStore, "values", {})
    monkeypatch.setattr(KeyStore, "written", [])
    monkeypatch.setitem(provider_module._REGISTRY, "aws", KeyStore)
    return render_fixture(tmp_path, "snowflake", deploy=True)


def _public_body(pem_private):
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem_private.encode(), password=None)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "".join(line for line in public.decode().splitlines() if not line.startswith("-----"))


def test_platform_snowflake_script_generates_the_provisioner_key_once(platform_snowflake):
    from vecten.mcp_onboarding import setup_script

    data = setup_script(platform_snowflake, "prod")
    assert KeyStore.written == ["MCP_PROVISIONER_PRIVATE_KEY"]
    body = _public_body(KeyStore.values["MCP_PROVISIONER_PRIVATE_KEY"])
    assert f"RSA_PUBLIC_KEY = '{body}'" in data["sql"]
    assert "-----" not in data["sql"] and "PRIVATE KEY" not in data["sql"]
    assert '"ACME"' in data["sql"] and '"ACME_PROD_WH"' in data["sql"]
    assert "ACME-PROD.snowflakecomputing.com" in data["sql"]
    assert data["commands"] == [
        "vecten mcp provisioner-script --env prod --confirm-run",
        "vecten github push",
    ]
    assert setup_script(platform_snowflake, "prod")["sql"] == data["sql"]
    assert KeyStore.written == ["MCP_PROVISIONER_PRIVATE_KEY"]


def test_confirming_the_platform_script_records_it_and_writes_no_key(platform_snowflake):
    from vecten.mcp_onboarding import setup_script
    from vecten_shared.config import load_config

    data = setup_script(platform_snowflake, "prod", confirm=True)
    assert "sql" not in data and data["confirmed"] and data["env"] == "prod"
    assert data["commands"] == ["vecten github push"]
    assert load_config(platform_snowflake).environments["prod"].provisioner_script_run
    assert KeyStore.written == []


@pytest.mark.parametrize(("env", "public_key"), [("local", None), ("prod", "AAAA")])
def test_platform_script_needs_an_env_and_makes_its_own_key(platform_snowflake, env, public_key):
    from vecten.mcp_onboarding import setup_script

    with pytest.raises(Failure) as caught:
        setup_script(platform_snowflake, env, public_key=public_key)
    assert caught.value.code == ExitCode.USAGE
    assert KeyStore.written == []


def test_a_postgres_platform_has_no_provisioner_script(tmp_path):
    from vecten.mcp_onboarding import setup_script

    from .fixture_project import render_fixture

    root = render_fixture(tmp_path, "postgres", deploy=True)
    with pytest.raises(Failure) as caught:
        setup_script(root, "prod")
    assert caught.value.code == ExitCode.REFUSED


def test_platform_script_grants_on_the_database_the_root_creates(platform_snowflake):
    from vecten.mcp_onboarding import setup_script
    from vecten_shared.config import load_config, save_config

    config = load_config(platform_snowflake)
    config.project.name = "acme-data"
    save_config(platform_snowflake, config)
    sql = setup_script(platform_snowflake, "prod")["sql"]
    assert '"ACME_DATA"' in sql and "ACME-DATA" not in sql


def _shared_dev(root, account="ACME-SHARED"):
    import re

    from vecten.deploy.tfvars import env_tfvars_path
    from vecten_shared.config import load_config, save_config

    prod = env_tfvars_path(root, "prod").read_text()
    dev = re.sub(r'environment\s*=\s*"prod"', 'environment = "dev"', prod)
    dev = re.sub(r'domain\s*=\s*"[^"]*"', 'domain = "dev.acme.com"', dev)
    dev = re.sub(r'snowflake_account\s*=\s*"[^"]*"', f'snowflake_account = "{account}"', dev)
    env_tfvars_path(root, "dev").write_text(dev)
    config = load_config(root)
    config.warehouse.shared_account = True
    save_config(root, config)


def test_a_shared_environments_script_names_its_own_objects_and_account(platform_snowflake):
    from vecten.mcp_onboarding import setup_script

    _shared_dev(platform_snowflake)
    sql = setup_script(platform_snowflake, "dev")["sql"]
    assert "CREATE ROLE IF NOT EXISTS ANALYST_ACME_DEV" in sql
    assert "EXTERNAL_OAUTH_ACME_DEV" in sql
    assert "'https://ACME-SHARED.snowflakecomputing.com'" in sql
    assert "'https://mcp.dev.acme.com'" in sql
    assert "ACME-PROD" not in sql


@pytest.fixture
def existing_snowflake(tmp_path):
    from .test_next_deploy import render_mcp_only_deploy

    return render_mcp_only_deploy(tmp_path, "snowflake://VECTEN_ADMIN@acct/analytics")


def _local(root, name):
    from vecten_shared.provider.local import LocalProvider

    return LocalProvider(root, inside_compose=False).get_secret(name)


def test_existing_snowflake_script_embeds_a_provisioner_key_it_generates(existing_snowflake):
    from vecten.mcp_onboarding import setup_script

    result = setup_script(existing_snowflake, "prod")
    stored = _local(existing_snowflake, "MCP_PROVISIONER_PRIVATE_KEY")
    assert "\n" not in stored
    body = public_body(load_private_key(stored, "K"))
    assert f"SET RSA_PUBLIC_KEY = '{body}';" in result["sql"]
    assert stored not in result["sql"]
    # The key is on the laptop already, so the environment gets a copy of it.
    assert result["commands"] == [
        "vecten mcp provisioner-script --env prod --confirm-run",
        "vecten secret set MCP_PROVISIONER_PRIVATE_KEY --env prod --from-local",
    ]


def test_a_second_existing_snowflake_script_keeps_the_key(existing_snowflake):
    from vecten.mcp_onboarding import setup_script

    setup_script(existing_snowflake, "prod")
    first = _local(existing_snowflake, "MCP_PROVISIONER_PRIVATE_KEY")
    setup_script(existing_snowflake, "prod")
    assert _local(existing_snowflake, "MCP_PROVISIONER_PRIVATE_KEY") == first


def test_a_given_public_key_wins_and_nothing_is_generated(existing_snowflake):
    from vecten.mcp_onboarding import setup_script
    from vecten_shared.keypair import generate_one_line

    own = public_body(load_private_key(generate_one_line(), "K"))
    result = setup_script(existing_snowflake, "prod", public_key=own)
    assert f"SET RSA_PUBLIC_KEY = '{own}';" in result["sql"]
    with pytest.raises(Failure):
        _local(existing_snowflake, "MCP_PROVISIONER_PRIVATE_KEY")
    assert result["commands"][1] == "vecten secret set MCP_PROVISIONER_PRIVATE_KEY --env prod"


def test_confirming_the_existing_snowflake_script_generates_nothing(existing_snowflake):
    from vecten.mcp_onboarding import setup_script

    result = setup_script(existing_snowflake, "prod", confirm=True)
    assert result["commands"] == [
        "vecten secret set MCP_PROVISIONER_PRIVATE_KEY --env prod --from-local"
    ]
    with pytest.raises(Failure):
        _local(existing_snowflake, "MCP_PROVISIONER_PRIVATE_KEY")
