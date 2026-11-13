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


def _commented(names):
    """The comment clause a shared account's objects carry, which tells them from another
    project's or environment's of the same name; the fixed layout's statements carry none."""
    return f" COMMENT = {_literal(names.comment)}" if names.suffix else ""


_SHOW = {"role": "ROLES", "user": "USERS", "integration": "SECURITY INTEGRATIONS"}


def _checked_block(names, changes):
    """`changes` inside one Snowflake Scripting block, which first stops, changing nothing,
    at an account-level object the script would create that another project or environment
    holds, and then stops at the first statement that fails, whatever the client running
    the script does after an error.

    `CREATE ... IF NOT EXISTS` would take such an object over. An object is the
    environment's when it carries the environment's comment and is owned by the role running
    the script or by the one that created the environment's database; a comment alone can
    be copied by any role that may create users or roles.
    """
    database = names.database.upper()
    checks = (
        ("role", _name(names.analyst_role)),
        ("role", _name(names.provisioner_role)),
        ("user", _name(names.provisioner_user)),
        ("integration", _name(names.integration)),
    )
    declarations = [
        "  found INTEGER DEFAULT 0;",
        "  foreign INTEGER DEFAULT 0;",
        "  owners INTEGER DEFAULT 0;",
        "  strangers INTEGER DEFAULT 0;",
        "  keeper VARCHAR;",
        "  owned VARCHAR;",
        "  held VARCHAR;",
        "  blocked VARCHAR;",
    ]
    body = [
        f"  SHOW DATABASES LIKE {_literal(database)};",
        '  SELECT MAX("owner") INTO :keeper FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))',
        f'    WHERE "name" = {_literal(database)};',
    ]
    for number, (kind, name) in enumerate(checks, start=1):
        message = (
            f"{kind} {name} exists without the comment {names.comment} or an owner of this "
            "environment: another project or environment holds the name, and this script "
            "changed nothing"
        )
        declarations.append(f"  taken_{number} EXCEPTION (-{20000 + number}, {_literal(message)});")
        body += [
            f"  SHOW {_SHOW[kind]} LIKE {_literal(name)};",
            # LIKE treats `_` as a wildcard, so the exact name is compared again.
            "  SELECT COUNT(*), COUNT_IF(COALESCE(\"comment\", '') <> "
            f"{_literal(names.comment)}) INTO :found, :foreign",
            f'    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = {_literal(name)};',
            "  IF (found > 0) THEN",
            f"    SHOW GRANTS ON {kind.upper()} {_ident(name)};",
            "    SELECT COUNT(*) INTO :owners FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
            "      WHERE \"privilege\" = 'OWNERSHIP'",
            '        AND "grantee_name" IN (CURRENT_ROLE(), :keeper);',
            f"    IF (foreign > 0 OR owners = 0) THEN RAISE taken_{number}; END IF;",
            "  END IF;",
        ]
    holders = _holder_checks(names)
    owners = _owner_checks(names)
    declarations += holders[0] + owners[0]
    body += holders[1] + owners[1]
    quoted = _single_quoted_bodies(changes)
    # The block is itself `$$`-quoted, so a `$$` inside it would end it early and run the
    # rest outside it.
    if "$$" in quoted:
        raise ValueError("a statement of the shared script holds $$, which would end its block")
    statements = "\n".join("  " + line if line else line for line in quoted.splitlines())
    return (
        "-- One block: it changes nothing when a name it creates is another's, and stops at\n"
        "-- the first statement that fails.\n"
        "EXECUTE IMMEDIATE $$\nDECLARE\n"
        + "\n".join(declarations)
        + "\nBEGIN\n"
        + "\n".join(body)
        + "\n"
        + statements
        + "\n  RETURN 'done';\nEND;\n$$;\n"
    )


def _holder_checks(names):
    """Declarations and statements that stop the block when the environment's provisioner or
    analyst role is held by anyone but the provisioner's user, the provisioner's role and
    what it owns, SYSADMIN and the deploy role (the database's owner, `keeper`): the
    Snowflake root grants every role it creates to SYSADMIN and to the role it runs as. A
    squatter could otherwise hold the role, then hand its ownership to either of them."""
    analyst, provisioner = _name(names.analyst_role), _name(names.provisioner_role)
    user = _name(names.provisioner_user)
    outside = "is held by a user or role outside this environment, and this script changed nothing"
    declarations = [
        f"  held_1 EXCEPTION (-20011, {_literal(f'role {provisioner} {outside}')});",
        f"  held_2 EXCEPTION (-20012, {_literal(f'role {analyst} {outside}')});",
    ]
    stranger = (
        'NOT (g."granted_to" = \'ROLE\' AND g."grantee_name" IN '
        f"({_literal(provisioner)}, 'SYSADMIN', :keeper))"
    )
    body = [
        f"  SHOW ROLES LIKE {_literal(provisioner)};",
        "  SELECT COUNT(*) INTO :found FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
        f'    WHERE "name" = {_literal(provisioner)};',
        "  IF (found > 0) THEN",
        f"    SHOW GRANTS TO ROLE {_ident(provisioner)};",
        "    owned := LAST_QUERY_ID();",
        f"    SHOW GRANTS OF ROLE {_ident(provisioner)};",
        "    SELECT COUNT(*) INTO :strangers FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
        f'      WHERE NOT (("granted_to" = \'USER\' AND "grantee_name" = {_literal(user)})',
        "        OR (\"granted_to\" = 'ROLE' AND \"grantee_name\" IN ('SYSADMIN', :keeper)));",
        "    IF (strangers > 0) THEN RAISE held_1; END IF;",
        "  END IF;",
        f"  SHOW ROLES LIKE {_literal(analyst)};",
        "  SELECT COUNT(*) INTO :found FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
        f'    WHERE "name" = {_literal(analyst)};',
        "  IF (found > 0) THEN",
        f"    SHOW GRANTS OF ROLE {_ident(analyst)};",
        "    held := LAST_QUERY_ID();",
        "    IF (owned IS NULL) THEN",
        "      SELECT COUNT(*) INTO :strangers FROM TABLE(RESULT_SCAN(:held)) g",
        f"        WHERE {stranger};",
        "    ELSE",
        "      SELECT COUNT(*) INTO :strangers FROM TABLE(RESULT_SCAN(:held)) g",
        f"        WHERE {stranger}",
        "          AND NOT EXISTS (SELECT 1 FROM TABLE(RESULT_SCAN(:owned)) o",
        '            WHERE o."privilege" = \'OWNERSHIP\' AND o."granted_on" = g."granted_to"',
        '              AND o."name" = g."grantee_name");',
        "    END IF;",
        "    IF (strangers > 0) THEN RAISE held_2; END IF;",
        "  END IF;",
    ]
    return declarations, body


def _owner_checks(names):
    """Declarations and statements that stop the block when the access schema or a sign-in
    policy in it exists under another owner than the role running the script: the loader
    may create schemas in the database, and CREATE ... IF NOT EXISTS would use its own."""
    database, schema = _ident(names.database.upper()), names.access_schema
    policies = (names.authentication_policy, names.service_policy)
    declarations = [
        f"  owner_{n} EXCEPTION (-{20020 + n}, "
        f"{_literal(f'{name} is owned by another role, and this script changed nothing')});"
        for n, name in enumerate((schema, *policies), start=1)
    ]
    body = [
        f"  SHOW SCHEMAS LIKE {_literal(schema)} IN DATABASE {database};",
        '  SELECT COUNT(*), COUNT_IF("owner" <> CURRENT_ROLE()) INTO :found, :foreign',
        f'    FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = {_literal(schema)};',
        "  IF (foreign > 0) THEN RAISE owner_1; END IF;",
        "  IF (found > 0) THEN",
    ]
    for n, policy in enumerate(policies, start=2):
        body += [
            f"    SHOW AUTHENTICATION POLICIES LIKE {_literal(policy)} IN SCHEMA "
            f"{database}.{_ident(schema)};",
            '    SELECT COUNT_IF("owner" <> CURRENT_ROLE()) INTO :foreign',
            f'      FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "name" = {_literal(policy)};',
            f"    IF (foreign > 0) THEN RAISE owner_{n}; END IF;",
        ]
    body.append("  END IF;")
    return declarations, body


def _single_quoted_bodies(text):
    """`text` with each `$$`-quoted procedure body as a single-quoted string, which a block
    that is itself `$$`-quoted can hold; Snowflake reads a doubled backslash or quote in it
    as one."""

    def quoted(match):
        body = match.group(1).replace("\\", "\\\\").replace("'", "''")
        return f"AS '{body}';"

    return re.sub(r"AS \$\$\n(.*?)\$\$;", quoted, text, flags=re.S)


def _sign_in_policies(names):
    """In a shared account, the policy that lets the environment's users sign in only through
    its own integration, the provisioner's right to set it on the users it creates, and the
    key-pair-only policy of the provisioner's own user."""
    if not names.suffix:
        return ""
    schema = (_ident(names.database.upper()), _ident(names.access_schema))
    policy = ".".join((*schema, _ident(names.authentication_policy)))
    service = ".".join((*schema, _ident(names.service_policy)))
    integration, user = _name(names.integration), _name(names.provisioner_user)
    comment = _literal(names.comment)
    return f"""CREATE AUTHENTICATION POLICY IF NOT EXISTS {policy}
  AUTHENTICATION_METHODS = ('OAUTH')
  SECURITY_INTEGRATIONS = ('{integration}')
  COMMENT = {comment};
ALTER AUTHENTICATION POLICY {policy} SET SECURITY_INTEGRATIONS = ('{integration}');
-- No token of this integration acts as SYSADMIN or the deploy role, which hold every role of
-- the environment; another environment's deploy user may carry no sign-in policy.
blocked := 'ALTER SECURITY INTEGRATION {integration} SET EXTERNAL_OAUTH_BLOCKED_ROLES_LIST = '
  || '(''SYSADMIN'', ''' || REPLACE(COALESCE(keeper, 'SYSADMIN'), '''', '''''') || ''')';
EXECUTE IMMEDIATE :blocked;
GRANT APPLY ON AUTHENTICATION POLICY {policy} TO ROLE {_name(names.provisioner_role)};
CREATE AUTHENTICATION POLICY IF NOT EXISTS {service}
  AUTHENTICATION_METHODS = ('KEYPAIR')
  COMMENT = {comment};
ALTER AUTHENTICATION POLICY {service} SET AUTHENTICATION_METHODS = ('KEYPAIR');
BEGIN
  -- A user holds one authentication policy, so a rerun replaces the one it set.
  ALTER USER {user} UNSET AUTHENTICATION POLICY;
EXCEPTION
  WHEN STATEMENT_ERROR THEN NULL;
END;
ALTER USER {user} SET AUTHENTICATION POLICY {service};
"""


# The version the installed procedures must report for every feature this server uses; it
# changes with their text, and an install that reports none is version 1.
ACCESS_VERSION = 4


def snowflake_access_helper(names):
    database = names.database.upper()
    provisioner = _name(names.provisioner_role)
    namespace = f"{_ident(database)}.{_ident(names.access_schema)}"
    signature = f"{namespace}.READ_GRANT(VARCHAR,VARCHAR,VARCHAR,BOOLEAN)"
    report = f"{namespace}.READ_GRANTS(VARCHAR)"
    handler = resources.files("vecten_shared.sql").joinpath("snowflake_access.js").read_text()
    constants = (
        f"var database = {_baked(database)};\n"
        f"var warehouse = {_baked(names.warehouse.upper())};\n"
        f"var analystRole = {_baked(_name(names.analyst_role))};\n"
        f"var provisionerRole = {_baked(provisioner)};\n"
        f"var loaderRole = {_baked(_name(names.loader_role))};\n"
        f"var suffix = {_baked(names.suffix)};\n"
        f"var version = {ACCESS_VERSION};\n"
    )
    metadata_query = _baked(
        f"SELECT COUNT(*) FROM {_ident(database)}.INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?"
    )
    return f"""CREATE SCHEMA IF NOT EXISTS {namespace}{_commented(names)};
REVOKE ALL PRIVILEGES ON SCHEMA {namespace} FROM ROLE PUBLIC;
GRANT USAGE ON SCHEMA {namespace} TO ROLE {provisioner};
CREATE OR REPLACE PROCEDURE {namespace}.READ_GRANT(
  TARGET VARCHAR, SCHEMA_NAME VARCHAR, RELATION_NAME VARCHAR, ENABLED BOOLEAN)
RETURNS VARCHAR LANGUAGE JAVASCRIPT EXECUTE AS OWNER AS $$
{constants}var mode = 'grant';
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
CREATE OR REPLACE PROCEDURE {namespace}.READ_GRANTS(TARGET VARCHAR)
RETURNS VARIANT LANGUAGE JAVASCRIPT EXECUTE AS OWNER AS $$
{constants}var mode = 'report';
{handler}
$$;
GRANT USAGE ON PROCEDURE {report} TO ROLE {provisioner};
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
    account = names.account if names is not None else None
    if kind != "snowflake" or not account or not re.fullmatch(r"[A-Za-z0-9_.-]+", account):
        raise ValueError("Snowflake setup needs an account locator")
    if public_key is not None and not re.fullmatch(r"[A-Za-z0-9+/=]+", public_key):
        raise ValueError("invalid provisioner public key")
    key = public_key or "<provisioner RSA public key without PEM headers>"
    analyst, provisioner = _name(names.analyst_role), _name(names.provisioner_role)
    user, integration = _name(names.provisioner_user), _name(names.integration)
    database = _ident(names.database.upper())
    # The analyst role's reads are the default group's grants in the MCP's state, which its
    # reconciliation applies; a grant here would undo a revoke made there on every rerun.
    grants = [f"GRANT USAGE ON DATABASE {database} TO ROLE {analyst};"]
    for role in (analyst, provisioner):
        grants.append(f"GRANT USAGE ON WAREHOUSE {_ident(names.warehouse.upper())} TO ROLE {role};")
    for schema in ("CORE", "MARTS"):
        target = f"{database}.{_ident(schema)}"
        grants.append(f"GRANT USAGE ON SCHEMA {target} TO ROLE {provisioner};")
        for shape in ("ALL", "FUTURE"):
            grants.append(
                f"GRANT SELECT ON {shape} TABLES IN SCHEMA {target} TO ROLE {provisioner} "
                "WITH GRANT OPTION;"
            )
    data_grants = "\n".join(grants)
    issuer = issuer.rstrip("/")
    shared = bool(names.suffix)
    if shared and not names.comment:
        raise ValueError("a shared account's script needs the environment's comment")
    comment = _commented(names)
    # A shared account's provisioner user keeps no second key or password, which its
    # key-pair-only policy would otherwise still admit beside the environment's own key.
    keys_only = (
        f"\nALTER USER {user} UNSET RSA_PUBLIC_KEY_2;\nALTER USER {user} UNSET PASSWORD;"
        if shared
        else ""
    )
    changes = f"""CREATE ROLE IF NOT EXISTS {analyst}{comment};
CREATE ROLE IF NOT EXISTS {provisioner}{comment};
GRANT CREATE USER ON ACCOUNT TO ROLE {provisioner};
GRANT CREATE ROLE ON ACCOUNT TO ROLE {provisioner};
GRANT ROLE {analyst} TO ROLE {provisioner} WITH GRANT OPTION;
CREATE USER IF NOT EXISTS {user} TYPE = SERVICE{comment};{keys_only}
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
  EXTERNAL_OAUTH_SCOPE_MAPPING_ATTRIBUTE = 'scope'{comment};
ALTER SECURITY INTEGRATION {integration} SET EXTERNAL_OAUTH_ANY_ROLE_MODE = ENABLE;
ALTER SECURITY INTEGRATION {integration} SET
  EXTERNAL_OAUTH_ISSUER = {_literal(issuer)}
  EXTERNAL_OAUTH_JWS_KEYS_URL = {_literal(issuer + "/jwks")}
  EXTERNAL_OAUTH_AUDIENCE_LIST = ({_literal(names.audience)});
{_sign_in_policies(names)}"""
    reports = f"""{_leftover_grants(names)}
-- The users and roles {provisioner} owns. On a running environment, or one whose state
-- snapshot was restored, they are the MCP's own: keep them. On a deploy after a destroy
-- without that restore, the new state knows none of them and the MCP refuses to start
-- beside them: run each DROP this prints, as ACCOUNTADMIN.
SHOW GRANTS TO ROLE {provisioner};
SELECT 'DROP ' || "granted_on" || ' "' || "name" || '";' AS leftover
FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE "privilege" = 'OWNERSHIP' AND \
("granted_on" = 'USER' OR ("granted_on" = 'ROLE' AND "name" <> {_literal(analyst)}));
"""
    header = "-- Run as ACCOUNTADMIN, or a role holding MANAGE GRANTS: the future grants need it.\n"
    if not shared:
        return header + changes + reports
    return header + _checked_block(names, changes) + reports


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
