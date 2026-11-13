"""Read-only catalog introspection and explicit administrator setup acknowledgements."""

import json
import re

from vecten.mcp_url import mcp_url
from vecten.secrets import provisioner_secret_names
from vecten_shared.config import EnvironmentFacts, load_config, save_config
from vecten_shared.identity_setup import introspection_document, provisioner_script
from vecten_shared.keypair import generate_one_line, load_private_key, public_body
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import snowflake_names
from vecten_shared.provider.local import LocalProvider
from vecten_shared.records import write_record, write_text_atomic

ADMIN_PASSWORD = "WAREHOUSE_ADMIN_PASSWORD"
ADMIN_PRIVATE_KEY = "WAREHOUSE_ADMIN_PRIVATE_KEY"
PROVISIONER_KEY = "MCP_PROVISIONER_PRIVATE_KEY"
# Snowflake's answer to a key-pair sign-in whose public key the user does not hold.
JWT_REFUSED = 390144


def existing_config(root):
    config = load_config(root)
    if not config.has("mcp") or config.has("platform") or not config.warehouse.existing:
        raise Failure(ExitCode.REFUSED, "this command requires an MCP-only existing warehouse")
    return config


def script_config(root):
    """The configuration of a project whose MCP identity an administrator's SQL creates: an
    MCP over an existing warehouse, or over a Snowflake warehouse the platform builds."""
    from vecten.deploy.snowflake import has_snowflake_root

    config = load_config(root)
    if config.has("mcp") and has_snowflake_root(config):
        return config
    return existing_config(root)


def _platform_public_key(root, env):
    """The provisioner's public key body, its pair generated into `env`'s secrets store once."""
    from vecten.deploy.snowflake import PROVISIONER_PRIVATE_KEY, ensure_key_pair
    from vecten.secrets import cloud_store

    store = cloud_store(root, env)
    pem = store.call(lambda: ensure_key_pair(store.provider, PROVISIONER_PRIVATE_KEY))
    return key_body(pem)


def setup_script(root, env, confirm=False, public_key=None):
    config = script_config(root)
    platform = config.has("platform")
    if platform and env == "local":
        raise Failure(
            ExitCode.USAGE,
            "the platform's Snowflake identity belongs to a deployed environment; pass --env",
        )
    if platform and public_key is not None:
        raise Failure(
            ExitCode.USAGE,
            "the platform generates the provisioner's key pair into the secrets store; "
            "drop --public-key",
        )
    if env != "local":
        from vecten_shared.names import check_env_name

        check_env_name(env)
        if not config.deploy:
            raise Failure(ExitCode.PREREQUISITE, "configure the deploy layer first")
    # Snowflake fetches the issuer's keys from the internet, which cannot reach the laptop, so
    # the laptop's script names prod's public issuer once a deploy layer exists.
    issuer = mcp_url(root, config, "prod" if env == "local" and config.deploy else env)
    if config.warehouse.kind == "snowflake" and not config.deploy:
        raise Failure(
            ExitCode.PREREQUISITE,
            "Snowflake External OAuth needs the deployed HTTPS issuer; "
            "configure the deploy layer first",
        )
    flag = f" --env {env}" if env != "local" else ""
    # On an existing Snowflake warehouse the laptop holds the provisioner's key, unless the
    # administrator brings a public key whose private half they keep themselves.
    generated = not platform and config.warehouse.kind == "snowflake" and public_key is None
    if platform:
        # The confirmation is a change to vecten.toml, and the push carrying it runs the
        # ship workflow, which ships the MCP once the identity is confirmed.
        commands = ["vecten github push"]
    elif generated:
        commands = [f"vecten secret set {PROVISIONER_KEY}{flag} --from-local"] if flag else []
    else:
        commands = [f"vecten secret set {name}{flag}" for name in provisioner_secret_names(config)]
    if confirm:
        write_record(root, f"provisioner-{env}", {"confirmed": True})
        if env != "local":
            facts = config.environments.setdefault(env, EnvironmentFacts())
            facts.provisioner_script_run = True
            save_config(root, config)
        return {"env": env, "confirmed": True, "commands": commands}
    if platform:
        public_key = _platform_public_key(root, env)
    elif generated:
        public_key = public_body(
            load_private_key(_local_key_pair(root, PROVISIONER_KEY), PROVISIONER_KEY)
        )
    snowflake = config.warehouse.kind == "snowflake"
    sql = provisioner_script(
        config.warehouse.kind,
        issuer=issuer,
        database=None if snowflake else config.warehouse.database or config.project.name,
        public_key=public_key,
        names=snowflake_names(config, env) if snowflake else None,
    )
    commands.insert(0, f"vecten mcp provisioner-script{flag} --confirm-run")
    return {"env": env, "sql": sql, "commands": commands, "confirmed": False}


# `provisioner-script` grants the MCP's roles on these schemas and no other.
CONVENTION_SCHEMAS = ("core", "marts")


def catalog_rows(root, config, schemas, user):
    # Snowflake stores an unquoted schema name in upper case.
    if config.warehouse.kind == "snowflake":
        schemas = [schema.upper() for schema in schemas]
    placeholders = ",".join(["%s"] * len(schemas))
    return _catalog_query(
        root,
        config,
        user,
        "SELECT table_catalog, table_schema, table_name, column_name, data_type "
        "FROM information_schema.columns WHERE table_schema IN (" + placeholders + ") "
        "ORDER BY table_catalog, table_schema, table_name, ordinal_position",
        schemas,
    )


def visible_schemas(root, config, user):
    """The schemas holding tables the administrator can see, system catalogs left out."""
    rows = _catalog_query(
        root,
        config,
        user,
        "SELECT DISTINCT table_schema FROM information_schema.tables "
        "WHERE lower(table_schema) NOT IN ('information_schema', 'pg_catalog') "
        "AND substr(lower(table_schema), 1, 3) <> 'pg_' ORDER BY table_schema",
        None,
    )
    return [row[0] for row in rows]


def _catalog_query(root, config, user, sql, params):
    warehouse = config.warehouse
    user = user or warehouse.admin_user
    conn = None
    try:
        if warehouse.kind == "postgres":
            import psycopg

            password = LocalProvider(root, inside_compose=False).get_secret(ADMIN_PASSWORD)
            conn = psycopg.connect(
                host=warehouse.host,
                port=warehouse.port or 5432,
                dbname=warehouse.database,
                user=user or "postgres",
                password=password,
                connect_timeout=10,
                sslmode="require",
                options="-c default_transaction_read_only=on -c statement_timeout=30000",
            )
        else:
            if not user:
                raise Failure(
                    ExitCode.USAGE,
                    "Snowflake introspection needs a user: name it in warehouse_url "
                    "(snowflake://<user>@<account>/<database>) or pass --user",
                )
            conn = _snowflake_admin(root, warehouse, user)
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Failure:
        raise
    except Exception as exc:
        raise Failure(
            ExitCode.TOOL_FAILED, f"warehouse catalog read failed ({type(exc).__name__})"
        ) from None
    finally:
        if conn:
            conn.close()


def _snowflake_admin(root, warehouse, user):
    """A connection as `user`, signed with the laptop's introspection key pair, which is
    generated on first use; Snowflake offers a service user no password, and a person's
    password sign-in meets MFA."""
    import snowflake.connector
    from snowflake.connector.errors import DatabaseError

    key = load_private_key(_local_key_pair(root, ADMIN_PRIVATE_KEY), ADMIN_PRIVATE_KEY)
    try:
        return snowflake.connector.connect(
            account=warehouse.account,
            user=user,
            private_key=key,
            database=warehouse.database,
            login_timeout=10,
        )
    except DatabaseError as exc:
        if getattr(exc, "errno", None) != JWT_REFUSED:
            raise
        raise Failure(
            ExitCode.PREREQUISITE,
            f"Snowflake refused the introspection key of {user}. As an administrator, create "
            "the user if it does not exist (TYPE = SERVICE, with a default role that reads the "
            "schemas and a default warehouse), then register the key: ALTER USER "
            f"{_user_identifier(user)} SET RSA_PUBLIC_KEY = '{public_body(key)}'; and run "
            "`vecten mcp introspect` again",
        ) from None


def _local_key_pair(root, name):
    """The laptop's private key `name`, generated into the local store when it holds none."""
    provider = LocalProvider(root, inside_compose=False)
    if name not in provider.list_secrets():
        provider.put_secret(name, generate_one_line())
    return provider.get_secret(name)


def key_body(pem):
    """A PEM key's body on one line, without its header and trailer, as Snowflake takes it."""
    return "".join(line.strip() for line in pem.splitlines() if not line.startswith("-----"))


def _user_identifier(user):
    # Snowflake folds an unquoted name to upper case, so a plain name is quoted folded.
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", user):
        user = user.upper()
    return '"' + user.replace('"', '""') + '"'


def _has_datasets(path):
    """Whether the model holds a dataset; `init` renders it with none. An unreadable file
    counts as holding one, so nothing a person wrote is replaced unasked."""
    try:
        document = json.loads(path.read_text())
        return any(model.get("datasets") for model in document.get("semantic_model", []))
    except ValueError, AttributeError, TypeError:
        return True


def introspect(root, schemas, user=None, replace=False):
    config = existing_config(root)
    path = root / "osi" / "datasets.json"
    if path.exists() and not replace and _has_datasets(path):
        raise Failure(
            ExitCode.REFUSED,
            "osi/datasets.json exists; use --replace after reviewing the existing model",
        )
    document = introspection_document(
        catalog_rows(root, config, schemas, user), config.warehouse.kind
    )
    datasets = document["semantic_model"][0]["datasets"]
    if not datasets:
        seen = ", ".join(visible_schemas(root, config, user)) or "none"
        raise Failure(
            ExitCode.PREREQUISITE,
            f"no visible tables in {', '.join(schemas)}; schemas with tables: {seen}. "
            "The MCP serves only the core and marts schemas: expose the tables to query "
            "as tables or views in marts",
        )
    warnings = [
        f"{schema} is outside core and marts: the MCP's provisioned roles cannot read it"
        for schema in schemas
        if schema.lower() not in CONVENTION_SCHEMAS
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(document, indent=2) + "\n")
    return {
        "path": "osi/datasets.json",
        "datasets": len(datasets),
        "needs_review": True,
        "warnings": warnings,
    }
