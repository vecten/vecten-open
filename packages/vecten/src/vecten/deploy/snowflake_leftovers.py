"""What a shared-account environment leaves in Snowflake outside its Terraform root.

The administrator's script creates the provisioner's user and the External OAuth integration,
and the provisioner creates a user per person and service and a role per group. They are
found by ownership, never by a name pattern: another environment's or project's suffix can
end in this one's (`_X_ACME_DEV` ends in `_ACME_DEV`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vecten.deploy.snowflake import DEPLOY_PRIVATE_KEY
from vecten_shared.keypair import load_private_key
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import SnowflakeNames


@dataclass(frozen=True)
class Leftovers:
    names: SnowflakeNames
    # (USER or ROLE, name) of every object the environment's provisioner role owns.
    owned: tuple[tuple[str, str], ...]
    # Whether the provisioner's user still holds the provisioner role.
    provisioner_user: bool

    @property
    def blocks_data_deletion(self) -> bool:
        """Users carry the environment's sign-in policies, which live in the database that
        data deletion drops, so they go first."""
        return bool(self.owned) or self.provisioner_user

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
            *(f'DROP {kind} "{name}";' for kind, name in self.owned),
            f"ALTER USER IF EXISTS {names.provisioner_user} UNSET AUTHENTICATION POLICY;",
            f"DROP USER IF EXISTS {names.provisioner_user};",
            f"DROP SECURITY INTEGRATION IF EXISTS {names.integration};",
        ]


def _rows(cursor: Any, query: str) -> list[dict[str, Any]]:
    rows = cursor.execute(query).fetchall()
    columns = [column[0].lower() for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def read_leftovers(
    credentials: dict[str, str],
    names: SnowflakeNames,
    *,
    connect: Callable[..., Any] | None = None,
    key: Callable[[str], Any] | None = None,
) -> Leftovers:
    """Read, as the deploy user, what the environment's provisioner owns and whether its user
    still holds the role; the deploy role owns that role, so it may read both."""
    if connect is None:
        import snowflake.connector

        connect = snowflake.connector.connect
    key = key or (lambda pem: load_private_key(pem, DEPLOY_PRIVATE_KEY))
    role = names.provisioner_role
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
            # LIKE treats `_` as a wildcard, so the exact name is compared again.
            roles = _rows(cursor, f"SHOW ROLES LIKE '{role}'")
            if not any(str(row["name"]).upper() == role for row in roles):
                return Leftovers(names=names, owned=(), provisioner_user=False)
            owned = tuple(
                (str(row["granted_on"]).upper(), str(row["name"]))
                for row in _rows(cursor, f'SHOW GRANTS TO ROLE "{role}"')
                if row["privilege"] == "OWNERSHIP"
                and str(row["granted_on"]).upper() in ("USER", "ROLE")
            )
            holders = _rows(cursor, f'SHOW GRANTS OF ROLE "{role}"')
            user = any(
                str(row["granted_to"]).upper() == "USER"
                and str(row["grantee_name"]).upper() == names.provisioner_user
                for row in holders
            )
        finally:
            cursor.close()
            connection.close()
    except Failure:
        raise
    except Exception as exc:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"could not read, as the deploy user, which users and roles {role} owns in "
            f"Snowflake: {type(exc).__name__}",
        ) from None
    return Leftovers(names=names, owned=owned, provisioner_user=user)
