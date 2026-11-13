"""Administrator-run setup SQL and credential-free semantic drafts."""

import json
import re
from collections import defaultdict
from importlib import resources
from urllib.parse import urlparse

from vecten_shared.osi import OSI_VERSION
from vecten_shared.osi.generate import classify


def _ident(value):
    if not value or "\x00" in value:
        raise ValueError("invalid identifier")
    return '"' + value.replace('"', '""') + '"'


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def postgres_access_helpers():
    """Return the same owner-scoped installer used by managed warehouse bootstrap."""
    from vecten_shared.sql import setup_sql_path

    sql = setup_sql_path().read_text()
    return sql.split("-- BEGIN VECTEN ACCESS HELPERS", 1)[1].split(
        "-- END VECTEN ACCESS HELPERS", 1
    )[0]


def _name(value):
    """An account-level name the script writes unquoted, as an administrator reads it; the
    Snowflake module's role variables accept the same names."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,254}", value or ""):
        raise ValueError(f"invalid Snowflake name {value!r}")
    return value


def _baked(value):
    # The value lands inside a $$-quoted procedure body, which a literal `$$` would end.
    return json.dumps(value).replace("$", "\\u0024")


def snowflake_access_helper(names):
    database = names.database.upper()
    provisioner = _name(names.provisioner_role)
    namespace = f"{_ident(database)}.{_ident(names.access_schema)}"
    signature = f"{namespace}.READ_GRANT(VARCHAR,VARCHAR,VARCHAR,BOOLEAN)"
    handler = resources.files("vecten_shared.sql").joinpath("snowflake_access.js").read_text()
    metadata_query = _baked(
        f"SELECT COUNT(*) FROM {_ident(database)}.INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
    )
    return f"""CREATE SCHEMA IF NOT EXISTS {namespace};
REVOKE ALL PRIVILEGES ON SCHEMA {namespace} FROM ROLE PUBLIC;
GRANT USAGE ON SCHEMA {namespace} TO ROLE {provisioner};
CREATE OR REPLACE PROCEDURE {namespace}.READ_GRANT(
  TARGET VARCHAR, SCHEMA_NAME VARCHAR, RELATION_NAME VARCHAR, ENABLED BOOLEAN)
RETURNS VARCHAR LANGUAGE JAVASCRIPT EXECUTE AS OWNER AS $$
var database = {_baked(database)};
var warehouse = {_baked(names.warehouse.upper())};
var analystRole = {_baked(_name(names.analyst_role))};
var provisionerRole = {_baked(provisioner)};
var loaderRole = {_baked(_name(names.loader_role))};
var suffix = {_baked(names.suffix)};
{handler}
$$;
GRANT USAGE ON PROCEDURE {signature} TO ROLE {provisioner};
CREATE OR REPLACE PROCEDURE {namespace}.SOURCE_EXISTS(SCHEMA_NAME VARCHAR, RELATION_NAME VARCHAR)
RETURNS BOOLEAN LANGUAGE JAVASCRIPT EXECUTE AS OWNER AS $$
if (typeof SCHEMA_NAME !== 'string' || typeof RELATION_NAME !== 'string'
    || SCHEMA_NAME.length > 255 || RELATION_NAME.length > 255) {{
  throw new Error('invalid relation name');
}}
var result = snowflake.execute({{sqlText: {metadata_query},
  binds: [SCHEMA_NAME, RELATION_NAME]}});
result.next();
return result.getColumnValue(1) > 0;
$$;
GRANT USAGE ON PROCEDURE {namespace}.SOURCE_EXISTS(VARCHAR,VARCHAR) TO ROLE {provisioner};
"""


_POSTGRES_HEADER = (
    "-- Run with psql 15 or later (\\getenv), as the warehouse administrator, with "
    "MCP_PROVISIONER_PASSWORD and MCP_IMPERSONATOR_PASSWORD set in the environment.\n"
)


def provisioner_script(kind, *, issuer, database=None, public_key=None, names=None):
    """The administrator's setup SQL: Postgres takes the warehouse's `database`, Snowflake
    every name from `names`."""
    if urlparse(issuer).scheme != "https" or not urlparse(issuer).hostname:
        raise ValueError("MCP issuer must be an HTTPS URL")
    if kind == "postgres":
        return (
            _POSTGRES_HEADER
            + f"""\\set ON_ERROR_STOP on
\\getenv provisioner_password MCP_PROVISIONER_PASSWORD
\\getenv impersonator_password MCP_IMPERSONATOR_PASSWORD
SELECT 'CREATE ROLE analyst NOLOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='analyst') \\gexec
SELECT format('CREATE ROLE provisioner LOGIN CREATEROLE PASSWORD %L', :'provisioner_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='provisioner') \\gexec
SELECT format('CREATE ROLE impersonator LOGIN NOINHERIT PASSWORD %L', :'impersonator_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='impersonator') \\gexec
GRANT analyst TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
GRANT CONNECT ON DATABASE {_ident(database)} TO analyst, provisioner, impersonator;
SELECT format('GRANT USAGE ON SCHEMA %I TO analyst, provisioner', nspname)
FROM pg_namespace WHERE nspname IN ('core','marts') \\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO analyst', nspname)
FROM pg_namespace WHERE nspname IN ('core','marts') \\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO provisioner WITH GRANT OPTION', nspname)
FROM pg_namespace WHERE nspname IN ('core','marts') \\gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO analyst',
              r.rolname,n.nspname)
FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname IN ('core','marts') \\gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
              'GRANT SELECT ON TABLES TO provisioner WITH GRANT OPTION',
              r.rolname,n.nspname)
FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname IN ('core','marts') \\gexec
"""
            + postgres_access_helpers()
        )
    account = names.account if names is not None else None
    if kind != "snowflake" or not account or not re.fullmatch(r"[A-Za-z0-9_.-]+", account):
        raise ValueError("Snowflake setup needs an account locator")
    if public_key is not None and not re.fullmatch(r"[A-Za-z0-9+/=]+", public_key):
        raise ValueError("invalid provisioner public key")
    key = public_key or "<provisioner RSA public key without PEM headers>"
    analyst, provisioner = _name(names.analyst_role), _name(names.provisioner_role)
    user, integration = _name(names.provisioner_user), _name(names.integration)
    database = _ident(names.database.upper())
    grants = [f"GRANT USAGE ON DATABASE {database} TO ROLE {analyst};"]
    for role in (analyst, provisioner):
        grants.append(f"GRANT USAGE ON WAREHOUSE {_ident(names.warehouse.upper())} TO ROLE {role};")
    for schema in ("CORE", "MARTS"):
        target = f"{database}.{_ident(schema)}"
        for role in (analyst, provisioner):
            grants.append(f"GRANT USAGE ON SCHEMA {target} TO ROLE {role};")
            suffix = " WITH GRANT OPTION" if role == provisioner else ""
            for shape in ("ALL", "FUTURE"):
                grants.append(
                    f"GRANT SELECT ON {shape} TABLES IN SCHEMA {target} TO ROLE {role}{suffix};"
                )
    data_grants = "\n".join(grants)
    issuer = issuer.rstrip("/")
    return f"""-- Run as ACCOUNTADMIN, or a role holding MANAGE GRANTS: the future grants need it.
CREATE ROLE IF NOT EXISTS {analyst};
CREATE ROLE IF NOT EXISTS {provisioner};
GRANT CREATE USER ON ACCOUNT TO ROLE {provisioner};
GRANT CREATE ROLE ON ACCOUNT TO ROLE {provisioner};
GRANT ROLE {analyst} TO ROLE {provisioner} WITH GRANT OPTION;
CREATE USER IF NOT EXISTS {user} TYPE = SERVICE;
ALTER USER {user} SET RSA_PUBLIC_KEY = {_literal(key)};
GRANT ROLE {provisioner} TO USER {user};
GRANT USAGE ON DATABASE {database} TO ROLE {provisioner};
{data_grants}
{snowflake_access_helper(names)}
CREATE SECURITY INTEGRATION IF NOT EXISTS {integration}
  TYPE = EXTERNAL_OAUTH
  ENABLED = TRUE
  EXTERNAL_OAUTH_TYPE = CUSTOM
  EXTERNAL_OAUTH_ISSUER = {_literal(issuer)}
  EXTERNAL_OAUTH_JWS_KEYS_URL = {_literal(issuer + "/jwks")}
  EXTERNAL_OAUTH_AUDIENCE_LIST = ({_literal(names.audience)})
  EXTERNAL_OAUTH_ANY_ROLE_MODE = ENABLE
  EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = ('sub')
  EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME'
  EXTERNAL_OAUTH_SCOPE_MAPPING_ATTRIBUTE = 'scope';
ALTER SECURITY INTEGRATION {integration} SET EXTERNAL_OAUTH_ANY_ROLE_MODE = ENABLE;
ALTER SECURITY INTEGRATION {integration} SET
  EXTERNAL_OAUTH_ISSUER = {_literal(issuer)}
  EXTERNAL_OAUTH_JWS_KEYS_URL = {_literal(issuer + "/jwks")}
  EXTERNAL_OAUTH_AUDIENCE_LIST = ({_literal(names.audience)});
{_leftover_grants(names)}
-- The users and roles {provisioner} owns. On a running environment, or one whose state
-- snapshot was restored, they are the MCP's own: keep them. On a deploy after a destroy
-- without that restore, the new state knows none of them and the MCP refuses to start
-- beside them: run each DROP this prints, as ACCOUNTADMIN.
SHOW GRANTS TO ROLE {provisioner};
SELECT 'DROP ' || "granted_on" || ' "' || "name" || '";' AS leftover
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "privilege" = 'OWNERSHIP' AND \
("granted_on" = 'USER' OR ("granted_on" = 'ROLE' AND "name" <> {_literal(analyst)}));
"""


def _leftover_grants(names):
    """The query printing what an earlier install granted the provisioner outside the
    environment's database and warehouse, which the MCP's startup check refuses."""
    database, warehouse = names.database.upper(), names.warehouse.upper()
    provisioner = _name(names.provisioner_role)
    outside = [
        f'"name" <> {_literal(database)}',
        f'NOT STARTSWITH("name", {_literal(database + ".")})',
        f'"name" <> {_literal(warehouse)}',
    ]
    access = _literal(f"%.{names.access_schema}.%")
    return (
        f"-- Grants {provisioner} holds outside this database and warehouse, from an earlier\n"
        "-- install: the MCP refuses to start beside them, so run each statement this prints,\n"
        f"-- as ACCOUNTADMIN. Procedure grants go with that install's {names.access_schema} "
        "schema.\n"
        "-- A REVOKE that leaves a table grant in place was made by the table's owner:\n"
        "-- GRANT OWNERSHIP ON TABLE <name> TO ROLE ACCOUNTADMIN REVOKE CURRENT GRANTS.\n"
        f"SHOW GRANTS TO ROLE {provisioner};\n"
        "SELECT DISTINCT CASE WHEN \"granted_on\" = 'PROCEDURE'\n"
        "  THEN 'DROP SCHEMA IF EXISTS ' || SPLIT_PART(\"name\", '.', 1) || "
        f"'.{names.access_schema};'\n"
        "  ELSE 'REVOKE ' || \"privilege\" || ' ON ' || \"granted_on\" || ' ' || \"name\" "
        f"|| ' FROM ROLE {provisioner};' END AS leftover_grant\n"
        "FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))\n"
        "WHERE (\"granted_on\" IN ('DATABASE', 'SCHEMA', 'TABLE', 'VIEW', 'WAREHOUSE')\n"
        f'  OR ("granted_on" = \'PROCEDURE\' AND "name" ILIKE {access}))\n'
        "  AND " + " AND ".join(outside) + ";"
    )


def introspection_document(rows, kind):
    dialect = "SNOWFLAKE" if kind == "snowflake" else "ANSI_SQL"
    tables = defaultdict(list)
    for database, schema, table, column, data_type in rows:
        if schema.lower() in {"information_schema", "pg_catalog"}:
            continue
        tables[(database, schema, table)].append((column, data_type))
    datasets, names = [], set()
    for (database, schema, table), columns in sorted(tables.items()):
        base = re.sub("[^a-zA-Z0-9_]", "_", table).lower()
        name = (
            base if base not in names else re.sub("[^a-zA-Z0-9_]", "_", schema).lower() + "_" + base
        )
        if name in names:
            raise ValueError("introspection dataset names collide across schemas")
        names.add(name)
        fields = []
        for column, data_type in columns:
            classification = classify(column, data_type)
            field = {
                "name": column,
                "expression": {"dialects": [{"dialect": dialect, "expression": _ident(column)}]},
            }
            if classification != "fact":
                field["dimension"] = {"is_time": classification == "time"}
            fields.append(field)
        datasets.append(
            {
                "name": name,
                "description": "",
                "source": ".".join((database, schema, table)),
                "fields": fields,
            }
        )
    return {
        "version": OSI_VERSION,
        "semantic_model": [{"name": "warehouse_draft", "datasets": datasets}],
    }
