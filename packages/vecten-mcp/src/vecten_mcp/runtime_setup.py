"""Assemble runtime dependencies from project configuration and scoped secret providers."""

import logging
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal

from sqlalchemy import URL

from vecten_mcp.access import AccessService
from vecten_mcp.administration import Administration
from vecten_mcp.auth.identity import OIDCBackend, PasswordBackend, digest
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.failures import NoWarehouseAccess, describe_error
from vecten_mcp.runtime import Runtime
from vecten_mcp.state import StateStore
from vecten_mcp.warehouse.pool import PrincipalPools
from vecten_mcp.warehouse.postgres import PostgresAdapter
from vecten_mcp.warehouse.snowflake import SnowflakeAdapter
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import get_provider, shipped_account, snowflake_names
from vecten_shared.provider.runtime import RUNTIME_WRITABLE
from vecten_shared.records import write_text_atomic

logger = logging.getLogger(__name__)

_NO_ROLE = re.compile(r'role ".*" does not exist')


def login_failure_kind(message: str) -> Literal["password", "no_role", "other"]:
    # psycopg gives no sqlstate for a failed connection, so libpq's text is the only signal.
    if "password authentication failed" in message:
        return "password"
    if _NO_ROLE.search(message):
        return "no_role"
    return "other"


def connect_role[T](role: str, candidates: Iterable[str], attempt: Callable[[str, str], T]) -> T:
    import psycopg

    kinds = []
    failures = []
    for password in candidates:
        try:
            connection = attempt(role, password)
        except psycopg.OperationalError as exc:
            failures.append(exc)
            kinds.append(login_failure_kind(str(exc)))
            continue
        # During a rotation the current password fails before the pending one connects.
        for exc in failures:
            logger.debug("warehouse login as %s failed: %s", role, describe_error(exc))
        return connection
    # The host appears in the driver text, so it goes to the log and never into the
    # ValueError that reaches CLI and MCP clients.
    for exc in failures:
        logger.warning("warehouse login as %s failed: %s", role, describe_error(exc))
    if "no_role" in kinds:
        message = f"the warehouse has no login `{role}`"
    elif kinds and all(kind == "password" for kind in kinds):
        # SCRAM and md5 servers answer a missing role as a failed password.
        message = (
            f"the warehouse refused the login `{role}`: "
            "the role does not exist or its password does not match"
        )
    else:
        message = f"could not connect to the warehouse as `{role}`"
    raise ValueError(message) from (failures[-1] if failures else None)


class LocalMcpSecrets:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def list_secrets(self):
        return sorted(
            {p.name for p in self.directory.iterdir() if p.name in RUNTIME_WRITABLE}
            | {name for name in os.environ if name.startswith("MCP_") and os.environ[name]}
        )

    def get_secret(self, name):
        if name in RUNTIME_WRITABLE and (self.directory / name).is_file():
            return (self.directory / name).read_text()
        value = os.environ.get(name)
        if not value:
            raise Failure(ExitCode.PREREQUISITE, f"required secret {name} is not set")
        return value

    def put_secret(self, name, value):
        if name not in RUNTIME_WRITABLE:
            raise ValueError("runtime secret write is outside the MCP rotation scope")
        write_text_atomic(self.directory / name, value, mode=0o600)


def optional_secret(provider, name):
    try:
        return provider.get_secret(name)
    except Failure as exc:
        if exc.code != ExitCode.PREREQUISITE:
            raise
        return None


def provisioner_key(secrets):
    """The provisioner's Snowflake key: PEM where the platform generated it, the one-line
    form where an existing warehouse's laptop did and `secret set --from-local` copied it."""
    from vecten_shared.keypair import load_private_key

    name = "MCP_PROVISIONER_PRIVATE_KEY"
    return load_private_key(secrets.get_secret(name), name)


def admin_link_reader(cloud, secrets):
    """Reads MCP_ADMIN_LINK afresh on every call, so a link issued while the server runs is
    seen; the local container reads secrets only at start, so it has no reader."""
    if cloud == "local":
        return None
    return lambda: optional_secret(secrets, "MCP_ADMIN_LINK")


def backend_name(mcp, env):
    if env == "local" and mcp.auth == "oidc" and not mcp.oidc_issuer and not mcp.oidc_client_id:
        return "password"
    return mcp.auth


def session_validator(state, clock):
    """The pool's check of a session and its principal, against durable state."""

    def validate_session(snapshot, session_id, roles):
        # Called while holding the pool lock. Revocation deletes durable state before
        # acquiring that same lock to close connections, so delayed callers cannot reopen it.
        session = state.get("sessions", session_id, now=clock())
        if not session or session["principal_id"] != snapshot["id"]:
            raise ValueError("warehouse session revoked")
        principal = state.principal(snapshot["id"])
        if not principal or not principal["enabled"] or not principal["provisioned"]:
            raise NoWarehouseAccess()
        current_roles = {
            state.group(group)["warehouse_role"] for group in state.groups_for(principal["id"])
        }
        return principal, current_roles

    return validate_session


def state_url(spec, password, cloud):
    """psycopg's default `prefer` would fall back to plain text on a deployed instance."""
    return URL.create(
        "postgresql+psycopg",
        username="mcp_state",
        password=password,
        host=spec.host,
        port=spec.port,
        database=spec.database,
        query={} if cloud == "local" else {"sslmode": "require"},
    )


def snowflake_adapter(config, env, secrets, keys, connect=None):
    """The Snowflake adapter: the provisioner's control connection and people's sessions,
    each signing in with the environment's names."""
    if connect is None:
        import snowflake.connector

        connect = snowflake.connector.connect
    names = snowflake_names(config, env, account=shipped_account())

    def provisioner():
        return connect(
            account=names.account,
            user=names.provisioner_user,
            role=names.provisioner_role,
            private_key=provisioner_key(secrets),
            database=names.database,
            warehouse=names.warehouse,
        )

    return SnowflakeAdapter(
        provisioner,
        connect,
        lambda **kwargs: keys.snowflake_token(**kwargs, audience=names.audience),
        names=names,
    )


def model_database(config, adapter):
    """(the database `osi/` sources name, the environment's database) on Snowflake, where a
    shared account gives each environment its own; None on Postgres."""
    if config.warehouse.kind != "snowflake":
        return None
    from vecten_shared.osi.generate import dataset_database

    return dataset_database(config), adapter.names.database


def configured_runtime(root, config, extensions):
    import psycopg

    env = os.environ.get("VECTEN_ENV", "local")
    cloud = os.environ.get("VECTEN_PROVIDER", "local")
    provider = get_provider(cloud, root, env)
    secrets = (
        LocalMcpSecrets(os.environ.get("VECTEN_MCP_SECRET_DIR", "/var/lib/vecten-mcp/secrets"))
        if cloud == "local"
        else provider
    )
    state = StateStore(
        state_url(
            provider.connection(env, "mcp_state"),
            secrets.get_secret("MCP_STATE_DB_PASSWORD"),
            cloud,
        )
    )
    issuer = os.environ.get("VECTEN_MCP_PUBLIC_URL") or (
        "https://mcp.localhost:8443" if env == "local" else f"https://mcp.{config.deploy.domain}"
    )
    keys = SigningKeys(secrets, issuer)
    selected = backend_name(config.mcp, env)
    if selected == "password":
        backend = PasswordBackend(
            state, admin_link=admin_link_reader(cloud, secrets), admins=config.mcp.admins
        )
    elif selected == "oidc":
        backend = OIDCBackend(
            state,
            config.mcp.oidc_issuer,
            config.mcp.oidc_client_id,
            issuer + "/oidc/callback",
            client_secret=optional_secret(secrets, "MCP_OIDC_CLIENT_SECRET"),
        )
    else:
        backend = extensions.identity_backend(selected)(
            config=config, state=state, provider=secrets
        )
    if config.warehouse.kind == "postgres":
        spec = provider.connection(env, "warehouse")
        host = config.warehouse.host if config.warehouse.existing else spec.host
        port = config.warehouse.port if config.warehouse.existing else spec.port
        database = config.warehouse.database if config.warehouse.existing else spec.database

        def connect_password(role, password):
            return psycopg.connect(
                host=host,
                port=port or 5432,
                dbname=database,
                user=role,
                password=password,
                autocommit=True,
                connect_timeout=10,
                sslmode="prefer" if cloud == "local" else "require",
            )

        def connect(role, name):
            from vecten_shared.credentials import password_candidates

            candidates = password_candidates(secrets.get_secret(name))
            return connect_role(role, candidates, connect_password)

        adapter = PostgresAdapter(
            lambda: connect("provisioner", "MCP_PROVISIONER_PASSWORD"),
            lambda: connect("impersonator", "MCP_IMPERSONATOR_PASSWORD"),
        )
    else:
        adapter = snowflake_adapter(config, env, secrets, keys)
    pools = PrincipalPools(adapter, max_connections=int(os.environ.get("VECTEN_MCP_POOL_CAP", "2")))
    oauth = OAuthService(
        state, keys, issuer, backend, admins=config.mcp.admins, revoke_session=pools.revoke_session
    )

    pools.validate = session_validator(state, oauth.clock)
    access = AccessService(state, adapter, invalidate=pools.disable_principal)
    access.seed_defaults()
    idle_setting = state.get("settings", "session_idle_seconds")
    if idle_setting:
        pools.idle_timeout = idle_setting["value"]
    admin = Administration(state, adapter, oauth, access, pools=pools)
    runtime = Runtime(state, oauth, extensions, adapter, pools, admin, access)
    from vecten_mcp.audit import AuditService

    runtime.audit = AuditService(state, start=False, sink=extensions.emit_audit)
    from vecten_mcp.queries import QueryService

    runtime.queries = QueryService(state, pools, kind=config.warehouse.kind)
    if config.warehouse.kind == "postgres":
        from vecten_mcp.rotation import PasswordRotation

        runtime.password_rotation = PasswordRotation(
            state, secrets, lambda password: connect_password("impersonator", password)
        )
    from vecten_mcp.model import ModelService
    from vecten_shared.osi.revision import read_built_commit

    runtime.model = ModelService(
        root / "osi",
        state,
        kind=config.warehouse.kind,
        source_exists=adapter.source_exists,
        platform=config.has("platform"),
        commit=lambda: os.environ.get("VECTEN_BUNDLE_COMMIT"),
        built=lambda: read_built_commit(os.environ),
        on_swap=lambda model: access.model_changed(
            {name: dataset["source"] for name, dataset in model.datasets.items()}
        ),
        database=model_database(config, adapter),
    )
    from vecten_mcp.feedback_resolution import FeedbackResolution

    manifest = os.environ.get("VECTEN_FEEDBACK_MANIFEST")
    runtime.feedback_resolution = FeedbackResolution(runtime, Path(manifest) if manifest else None)
    if runtime.model.current:
        access.datasets = {
            name: dataset["source"] for name, dataset in runtime.model.current.datasets.items()
        }
    if selected == "password" and config.mcp.admins:
        email = config.mcp.admins[0].lower()
        if not state.by_email(email):
            from vecten_mcp.identity import warehouse_name

            name = warehouse_name(
                email.split("@")[0], {p["warehouse_name"] for p in state.principals()}
            )
            state.invite(email, email, name)
        setup_token = optional_secret(secrets, "MCP_ADMIN_SETUP_TOKEN")
        if not state.get("passwords", email) and setup_token:
            state.put(
                "password_links",
                digest(setup_token),
                {"email": email, "principal_id": state.by_email(email)["id"]},
                expires_at=oauth.clock() + 900,
            )
    return runtime
