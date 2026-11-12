"""What a shared-account environment leaves in Snowflake outside its Terraform root.

The administrator's script creates the provisioner's user and the External OAuth integration,
and the provisioner creates a user per person and service and a role per group. They are
found by ownership, never by a name pattern: another environment's or project's suffix can
end in this one's (`_X_ACME_DEV` ends in `_ACME_DEV`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vecten.deploy.snowflake import DEPLOY_PRIVATE_KEY
from vecten_shared.keypair import load_private_key
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import SnowflakeNames

# The MCP creates only these names; a quote in one printed for an ACCOUNTADMIN could end it.
PLAIN_NAME = re.compile(r"[A-Z0-9_$]+")


@dataclass(frozen=True)
class Leftovers:
    names: SnowflakeNames
    # (USER or ROLE, name) of every object the environment's provisioner role owns.
    owned: tuple[tuple[str, str], ...]
    # Whether the provisioner's user exists; it carries the key-pair-only policy kept in the
    # environment's database.
    provisioner_user: bool
    # Whether the environment's External OAuth integration exists.
    integration: bool

    @property
    def blocks_data_deletion(self) -> bool:
        """Users carry the environment's sign-in policies, which live in the database that
        data deletion drops, so they go first, and the integration with them."""
        return bool(self.owned) or self.provisioner_user or self.integration

    def kept(self) -> list[str]:
        """The objects that stay in Snowflake while the database is kept."""
        names = self.names
        return [
            f"user {names.provisioner_user}",
            f"security integration {names.integration}",
            *(f"{kind.lower()} {name}" for kind, name in self.owned),
        ]

    def statements(self) -> list[str]:
        """What an ACCOUNTADMIN runs before data deletion, in order."""
        names = self.names
        return [
            *(f"DROP {kind} {_quoted(name)};" for kind, name in self.owned),
            f"ALTER USER IF EXISTS {names.provisioner_user} UNSET AUTHENTICATION POLICY;",
            f"DROP USER IF EXISTS {names.provisioner_user};",
            f"DROP SECURITY INTEGRATION IF EXISTS {names.integration};",
        ]


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _rows(cursor: Any, query: str) -> list[dict[str, Any]]:
    rows = cursor.execute(query).fetchall()
    columns = [column[0].lower() for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _exists(cursor: Any, show: str, name: str) -> bool:
    # LIKE treats `_` as a wildcard, so the exact name is compared again.
    return any(str(row["name"]).upper() == name for row in _rows(cursor, f"{show} LIKE '{name}'"))


def _read(cursor: Any, names: SnowflakeNames) -> Leftovers:
    role = names.provisioner_role
    user = _exists(cursor, "SHOW USERS", names.provisioner_user)
    integration = _exists(cursor, "SHOW SECURITY INTEGRATIONS", names.integration)
    if not _exists(cursor, "SHOW ROLES", role):
        return Leftovers(names=names, owned=(), provisioner_user=user, integration=integration)
    owned = tuple(
        (str(row["granted_on"]).upper(), str(row["name"]))
        for row in _rows(cursor, f'SHOW GRANTS TO ROLE "{role}"')
        if row["privilege"] == "OWNERSHIP" and str(row["granted_on"]).upper() in ("USER", "ROLE")
    )
    strange = [(kind, name) for kind, name in owned if not PLAIN_NAME.fullmatch(name)]
    if strange:
        kind, name = strange[0]
        raise Failure(
            ExitCode.REFUSED,
            f"{role} owns a {kind.lower()} whose name is no plain upper-case identifier, "
            f"{json.dumps(name)}, which the MCP never creates; nothing is printed to run. "
            f"Inspect it with SHOW GRANTS TO ROLE {role} and remove it by hand",
        )
    holders = _rows(cursor, f'SHOW GRANTS OF ROLE "{role}"')
    user = user or any(
        str(row["granted_to"]).upper() == "USER"
        and str(row["grantee_name"]).upper() == names.provisioner_user
        for row in holders
    )
    return Leftovers(names=names, owned=owned, provisioner_user=user, integration=integration)


def read_leftovers(
    credentials: dict[str, str],
    names: SnowflakeNames,
    *,
    connect: Callable[..., Any] | None = None,
    key: Callable[[str], Any] | None = None,
) -> Leftovers:
    """Read, as the deploy user, what the environment's provisioner owns and whether its user
    and integration stay; the deploy role owns the provisioner role and holds MANAGE GRANTS."""
    if connect is None:
        import snowflake.connector

        connect = snowflake.connector.connect
    key = key or (lambda pem: load_private_key(pem, DEPLOY_PRIVATE_KEY))
    account = (
        f"{credentials['SNOWFLAKE_ORGANIZATION_NAME']}-{credentials['SNOWFLAKE_ACCOUNT_NAME']}"
    )
    try:
        connection = connect(
            account=account,
            user=credentials["SNOWFLAKE_USER"],
            private_key=key(credentials["SNOWFLAKE_PRIVATE_KEY"]),
            login_timeout=10,
        )
        cursor = connection.cursor()
        try:
            return _read(cursor, names)
        finally:
            cursor.close()
            connection.close()
    except Failure:
        raise
    except Exception as exc:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"could not read, as the deploy user, which users and roles "
            f"{names.provisioner_role} owns in Snowflake: {type(exc).__name__}",
        ) from None
