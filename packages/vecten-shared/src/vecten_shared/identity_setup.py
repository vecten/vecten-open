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


# An install from before the procedures reported a version is version 1.
ACCESS_VERSION = 2


def snowflake_access_helper(database, warehouse=None):
    database = database.upper()
    warehouse = warehouse.upper() if warehouse else None
    namespace = f'{_ident(database)}."VECTEN_ACCESS"'
    signature = f"{namespace}.READ_GRANT(VARCHAR,VARCHAR,VARCHAR,BOOLEAN)"
    report = f"{namespace}.READ_GRANTS(VARCHAR)"
    handler = resources.files("vecten_shared.sql").joinpath("snowflake_access.js").read_text()
    values = [json.dumps(value).replace("$", "\\u0024") for value in (database, warehouse)]
    constants = f"var database = {values[0]};\nvar warehouse = {values[1]};\n"
    constants += f"var version = {ACCESS_VERSION};\n"
    metadata_query = json.dumps(
        f"SELECT COUNT(*) FROM {_ident(database)}.INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
    ).replace("$", "\\u0024")
    return f"""CREATE SCHEMA IF NOT EXISTS {namespace};
REVOKE ALL PRIVILEGES ON SCHEMA {namespace} FROM ROLE PUBLIC;
GRANT USAGE ON SCHEMA {namespace} TO ROLE PROVISIONER;
CREATE OR REPLACE PROCEDURE {namespace}.READ_GRANT(
  TARGET VARCHAR, SCHEMA_NAME VARCHAR, RELATION_NAME VARCHAR, ENABLED BOOLEAN)
RETURNS VARCHAR LANGUAGE JAVASCRIPT EXECUTE AS OWNER AS $$
{constants}var mode = 'grant';
{handler}
$$;
GRANT USAGE ON PROCEDURE {signature} TO ROLE PROVISIONER;
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
GRANT USAGE ON PROCEDURE {namespace}.SOURCE_EXISTS(VARCHAR,VARCHAR) TO ROLE PROVISIONER;
CREATE OR REPLACE PROCEDURE {namespace}.READ_GRANTS(TARGET VARCHAR)
RETURNS VARIANT LANGUAGE JAVASCRIPT EXECUTE AS OWNER AS $$
{constants}var mode = 'report';
{handler}
$$;
GRANT USAGE ON PROCEDURE {report} TO ROLE PROVISIONER;
"""


_POSTGRES_HEADER = (
    "-- Run with psql 15 or later (\\getenv), as the warehouse administrator, with "
    "MCP_PROVISIONER_PASSWORD and MCP_IMPERSONATOR_PASSWORD set in the environment.\n"
)


def provisioner_script(kind, *, database, issuer, account=None, public_key=None, warehouse=None):
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
SELECT format('GRANT USAGE ON SCHEMA %I TO provisioner', nspname)
FROM pg_namespace WHERE nspname IN ('core','marts') \\gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO provisioner WITH GRANT OPTION', nspname)
FROM pg_namespace WHERE nspname IN ('core','marts') \\gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I '
              'GRANT SELECT ON TABLES TO provisioner WITH GRANT OPTION',
              r.rolname,n.nspname)
FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname IN ('core','marts') \\gexec
"""
            + postgres_access_helpers()
        )
    if kind != "snowflake" or not account or not re.fullmatch(r"[A-Za-z0-9_.-]+", account):
        raise ValueError("Snowflake setup needs an account locator")
    if public_key is not None and not re.fullmatch(r"[A-Za-z0-9+/=]+", public_key):
        raise ValueError("invalid provisioner public key")
    key = public_key or "<provisioner RSA public key without PEM headers>"
    # ANALYST's reads are the default group's grants in the MCP's state, which its
    # reconciliation applies; a grant here would undo a revoke made there on every rerun.
    grants = [f"GRANT USAGE ON DATABASE {_ident(database.upper())} TO ROLE ANALYST;"]
    if warehouse:
        for role in ("ANALYST", "PROVISIONER"):
            grants.append(f"GRANT USAGE ON WAREHOUSE {_ident(warehouse.upper())} TO ROLE {role};")
    for schema in ("CORE", "MARTS"):
        target = f"{_ident(database.upper())}.{_ident(schema)}"
        grants.append(f"GRANT USAGE ON SCHEMA {target} TO ROLE PROVISIONER;")
        for shape in ("ALL", "FUTURE"):
            grants.append(
                f"GRANT SELECT ON {shape} TABLES IN SCHEMA {target} TO ROLE PROVISIONER "
                "WITH GRANT OPTION;"
            )
    data_grants = "\n".join(grants)
    helper = snowflake_access_helper(database, warehouse)
    leftover_grants = _leftover_grants(database.upper(), warehouse.upper() if warehouse else None)
    return f"""-- Run as ACCOUNTADMIN, or a role holding MANAGE GRANTS: the future grants need it.
CREATE ROLE IF NOT EXISTS ANALYST;
CREATE ROLE IF NOT EXISTS PROVISIONER;
GRANT CREATE USER ON ACCOUNT TO ROLE PROVISIONER;
GRANT CREATE ROLE ON ACCOUNT TO ROLE PROVISIONER;
GRANT ROLE ANALYST TO ROLE PROVISIONER WITH GRANT OPTION;
CREATE USER IF NOT EXISTS VECTEN_PROVISIONER TYPE = SERVICE;
ALTER USER VECTEN_PROVISIONER SET RSA_PUBLIC_KEY = {_literal(key)};
GRANT ROLE PROVISIONER TO USER VECTEN_PROVISIONER;
GRANT USAGE ON DATABASE {_ident(database.upper())} TO ROLE PROVISIONER;
{data_grants}
{helper}
CREATE SECURITY INTEGRATION IF NOT EXISTS VECTEN_EXTERNAL_OAUTH
  TYPE = EXTERNAL_OAUTH
  ENABLED = TRUE
  EXTERNAL_OAUTH_TYPE = CUSTOM
  EXTERNAL_OAUTH_ISSUER = {_literal(issuer.rstrip("/"))}
  EXTERNAL_OAUTH_JWS_KEYS_URL = {_literal(issuer.rstrip("/") + "/jwks")}
  EXTERNAL_OAUTH_AUDIENCE_LIST = ({_literal("https://" + account + ".snowflakecomputing.com")})
  EXTERNAL_OAUTH_ANY_ROLE_MODE = ENABLE
  EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = ('sub')
  EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME'
  EXTERNAL_OAUTH_SCOPE_MAPPING_ATTRIBUTE = 'scope';
ALTER SECURITY INTEGRATION VECTEN_EXTERNAL_OAUTH SET EXTERNAL_OAUTH_ANY_ROLE_MODE = ENABLE;
ALTER SECURITY INTEGRATION VECTEN_EXTERNAL_OAUTH SET
  EXTERNAL_OAUTH_ISSUER = {_literal(issuer.rstrip("/"))}
  EXTERNAL_OAUTH_JWS_KEYS_URL = {_literal(issuer.rstrip("/") + "/jwks")}
  EXTERNAL_OAUTH_AUDIENCE_LIST = ({_literal("https://" + account + ".snowflakecomputing.com")});
{leftover_grants}
-- The users and roles PROVISIONER owns. On a running environment, or one whose state
-- snapshot was restored, they are the MCP's own: keep them. On a deploy after a destroy
-- without that restore, the new state knows none of them and the MCP refuses to start
-- beside them: run each DROP this prints, as ACCOUNTADMIN.
SHOW GRANTS TO ROLE PROVISIONER;
SELECT 'DROP ' || "granted_on" || ' "' || "name" || '";' AS leftover
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "privilege" = 'OWNERSHIP' AND \
("granted_on" = 'USER' OR ("granted_on" = 'ROLE' AND "name" <> 'ANALYST'));
"""


def _leftover_grants(database, warehouse):
    """The query printing what an earlier install granted PROVISIONER outside `database` and
    `warehouse`, which the MCP's startup check refuses."""
    outside = [
        f'"name" <> {_literal(database)}',
        f'NOT STARTSWITH("name", {_literal(database + ".")})',
    ]
    if warehouse:
        outside.append(f'"name" <> {_literal(warehouse)}')
    return (
        "-- Grants PROVISIONER holds outside this database and warehouse, from an earlier\n"
        "-- install: the MCP refuses to start beside them, so run each statement this prints,\n"
        "-- as ACCOUNTADMIN. Procedure grants go with that install's VECTEN_ACCESS schema.\n"
        "-- A REVOKE that leaves a table grant in place was made by the table's owner:\n"
        "-- GRANT OWNERSHIP ON TABLE <name> TO ROLE ACCOUNTADMIN REVOKE CURRENT GRANTS.\n"
        "SHOW GRANTS TO ROLE PROVISIONER;\n"
        "SELECT DISTINCT CASE WHEN \"granted_on\" = 'PROCEDURE'\n"
        "  THEN 'DROP SCHEMA IF EXISTS ' || SPLIT_PART(\"name\", '.', 1) || '.VECTEN_ACCESS;'\n"
        "  ELSE 'REVOKE ' || \"privilege\" || ' ON ' || \"granted_on\" || ' ' || \"name\" "
        "|| ' FROM ROLE PROVISIONER;' END AS leftover_grant\n"
        "FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))\n"
        "WHERE (\"granted_on\" IN ('DATABASE', 'SCHEMA', 'TABLE', 'VIEW', 'WAREHOUSE')\n"
        "  OR (\"granted_on\" = 'PROCEDURE' AND \"name\" ILIKE '%.VECTEN_ACCESS.%'))\n"
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
