"""`vecten user add-admin`: a person's administrator login on the warehouse instance, created
from the laptop over the instance's master credential, along the way the cloud's
`admin_connection` opens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vecten.deploy.guards import in_actions, require_bootstrapped, require_deployed
from vecten.deploy.run import control_plane, current_outputs
from vecten_shared.config import load_config
from vecten_shared.names import check_env_name
from vecten_shared.output import ExitCode, Failure

FIRST_NAME = re.compile(r"[a-z][a-z0-9_]{0,40}")
WAREHOUSE_DB = "warehouse"
WAREHOUSE_OWNER = "warehouse_owner"
SCRAM_ITERATIONS = 4096
REQUIRED_AUTH = "scram-sha-256"
# libpq reads `require_auth` from 16 on and refuses the connection string before that.
REQUIRE_AUTH_LIBPQ = 160000
APPLICATION_NAME = "vecten user add-admin"

Connect = Callable[[dict[str, Any]], Any]


def admin_role(firstname: str) -> str:
    name = firstname.lower()
    if not FIRST_NAME.fullmatch(name):
        raise Failure(
            ExitCode.USAGE,
            f"first name {firstname!r} must be a letter followed by up to 40 letters, digits "
            "or underscores",
        )
    return f"admin_{name}"


def admin_secret_name(role: str) -> str:
    return f"{role.upper()}_PASSWORD"


def scram_verifier(password: str, salt: bytes | None = None) -> str:
    """The SCRAM-SHA-256 verifier Postgres stores for `password` (RFC 5802, RFC 7677).

    Sending the verifier instead of the password keeps the password out of the server's
    statement logs and pg_stat_statements, which record utility statements verbatim.
    The generated passwords are ASCII, which SASLprep leaves unchanged.
    """
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, SCRAM_ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = base64.b64encode
    return (
        f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt).decode()}"
        f"${b64(stored_key).decode()}:{b64(server_key).decode()}"
    )


@dataclass(frozen=True)
class AdminOutcome:
    created: bool
    password_rotated: bool


def ensure_admin(
    conn: Any,
    role: str,
    store_password: Callable[[str], None],
    *,
    password_stored: bool,
    grants: tuple[str, ...],
) -> AdminOutcome:
    """Create `role` with full warehouse rights unless it exists, then grant every time.

    An existing role keeps its password while `password_stored` says the secrets store holds
    it; otherwise it gets a new one, since nobody could sign in with the old. A new password
    is stored before the statement that sets it, so a failed statement leaves a stored
    password the next run replaces. `role` is `admin_<name>` checked by `admin_role`, and
    `grants` are the cloud's fixed role names, so both are safe bare identifiers.
    """
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    created = exists is None
    rotated = not created and not password_stored
    if created or rotated:
        password = secrets.token_urlsafe(32)
        store_password(password)
        # A verifier holds only base64 and `$:`, so it needs no escaping inside the literal.
        verifier = scram_verifier(password)
        if created:
            conn.execute(f"CREATE ROLE {role} LOGIN CREATEROLE CREATEDB PASSWORD '{verifier}'")
        else:
            conn.execute(f"ALTER ROLE {role} PASSWORD '{verifier}'")
    conn.execute(f"GRANT {', '.join(grants)} TO {role}")
    # PUBLIC holds no CONNECT on the warehouse and the granted roles may give none. The master
    # holds the owner role with SET but without INHERIT, so it grants as the owner.
    conn.execute(f"SET ROLE {WAREHOUSE_OWNER}")
    conn.execute(f"GRANT CONNECT ON DATABASE {WAREHOUSE_DB} TO {role}")
    conn.execute("RESET ROLE")
    conn.commit()
    return AdminOutcome(created=created, password_rotated=rotated)


def connect_with_psycopg(kwargs: dict[str, Any]) -> Any:
    try:
        import psycopg
    except ImportError:
        # psycopg is a dependency of vecten, so only a damaged install lands here.
        raise Failure(
            ExitCode.PREREQUISITE,
            "the psycopg driver vecten depends on cannot be imported; reinstall vecten",
        ) from None
    if psycopg.pq.version() < REQUIRE_AUTH_LIBPQ:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"psycopg uses libpq {psycopg.pq.version()}, which cannot require SCRAM "
            "authentication; reinstall vecten so psycopg's bundled libpq (16 or later) is used",
        )
    try:
        return psycopg.connect(**kwargs)
    except psycopg.Error as exc:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"connecting to the warehouse instance failed: {type(exc).__name__}",
        ) from None


default_connect: Connect = connect_with_psycopg


def _statement_failure(exc: Exception) -> Failure:
    # A driver message can quote connection parameters, so only the type and the server's
    # primary message, which quotes no statement, are shown.
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None)
    detail = f": {primary}" if primary else ""
    return Failure(
        ExitCode.TOOL_FAILED, f"creating the admin account failed: {type(exc).__name__}{detail}"
    )


def add_admin(
    project_root: Path,
    firstname: str,
    env: str,
    *,
    connect: Connect | None = None,
) -> dict:
    """Create `admin_<firstname>` on the instance holding the warehouse database, storing its
    password as `ADMIN_<FIRSTNAME>_PASSWORD`.

    Every refusal that needs no cloud runs before the first cloud call.
    """
    check_env_name(env)
    role = admin_role(firstname)
    config = load_config(project_root)
    if config.warehouse.existing:
        raise Failure(
            ExitCode.REFUSED,
            "the admin already has an account on the existing warehouse; vecten creates no "
            "account there",
        )
    if config.warehouse.kind == "snowflake":
        raise Failure(
            ExitCode.REFUSED,
            "create the admin user in Snowflake; vecten user add-admin covers Postgres "
            "warehouses in this release",
        )
    provider = control_plane(config, project_root, env)
    require_bootstrapped(config, env)
    require_deployed(config, env)
    provider.require_admin_access()
    if not in_actions():
        provider.check_login()
    outputs = current_outputs(provider)
    if outputs is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"environment {env!r} has no stored outputs; run `vecten deploy --env {env}`",
        )
    holder = next(
        (inst for inst in outputs.postgres.values() if WAREHOUSE_DB in inst.databases), None
    )
    if holder is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"environment {env!r} has no warehouse instance in its outputs; "
            f"run `vecten deploy --env {env}`",
        )
    user, password = provider.master_credential(holder)
    secret = admin_secret_name(role)
    password_stored = secret in provider.list_secrets()
    with provider.admin_connection(outputs, holder) as reach:
        kwargs = {
            **reach,
            "dbname": "postgres",
            "user": user,
            "password": password,
            "connect_timeout": 15,
            "application_name": APPLICATION_NAME,
            # A server, or anything posing as one, that asks for the password in clear or by
            # MD5 gets no answer: the master password leaves only as a SCRAM proof.
            "require_auth": REQUIRED_AUTH,
        }
        try:
            connection = (connect or default_connect)(kwargs)
        except Failure:
            raise
        except Exception as exc:
            # A driver message can quote connection parameters, so only the type is shown.
            raise Failure(
                ExitCode.TOOL_FAILED, f"connecting to the warehouse failed: {type(exc).__name__}"
            ) from None
        try:
            with connection as conn:
                try:
                    outcome = ensure_admin(
                        conn,
                        role,
                        lambda value: provider.put_secret(secret, value),
                        password_stored=password_stored,
                        grants=provider.admin_grants,
                    )
                except Failure:
                    raise
                except Exception as exc:
                    raise _statement_failure(exc) from None
        except Failure:
            raise
        except Exception as exc:
            # Leaving the block commits and closes; a driver message can quote connection
            # parameters, so only the type is shown.
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"closing the warehouse connection failed: {type(exc).__name__}",
            ) from None
    return {
        "env": env,
        "role": role,
        "created": outcome.created,
        "password_rotated": outcome.password_rotated,
        "secret": secret,
        "secret_id": provider.secret_id(secret),
    }
