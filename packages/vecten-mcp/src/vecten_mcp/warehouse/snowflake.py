"""Snowflake users keep their own credentials; MCP sessions use External OAuth."""

from contextlib import contextmanager

from vecten_mcp.warehouse.base import Account, check_account


def quote(name):
    return '"' + name.upper().replace('"', '""') + '"'


class SnowflakeAdapter:
    def __init__(self, provisioner_connect, query_connect, mint_token, *, names):
        self.provisioner_connect, self.query_connect = provisioner_connect, query_connect
        self.mint_token = mint_token
        self.names = names
        self.account, self.database, self.warehouse = names.account, names.database, names.warehouse

    # The registry's name of the default group's role, which both warehouses seed.
    DEFAULT_ROLE = "analyst"

    def _role(self, role):
        """The Snowflake role a registry role name stands for; in a shared account it ends in
        the environment's suffix, which the registry never holds."""
        if role == self.DEFAULT_ROLE:
            return self.names.analyst_role
        return role.upper() + self.names.suffix

    def _user(self, name):
        """The Snowflake user a registry warehouse name stands for."""
        return name.upper() + self.names.suffix

    def _registry_name(self, name):
        """The registry's name for a Snowflake user or role of this environment; None for one
        without the environment's suffix, which belongs to no principal or group here."""
        suffix = self.names.suffix
        if not name.upper().endswith(suffix) or len(name) <= len(suffix):
            return None
        return name[: len(name) - len(suffix)].lower()

    def _policy(self):
        names = self.names
        return ".".join(
            quote(part)
            for part in (names.database, names.access_schema, names.authentication_policy)
        )

    @contextmanager
    def control(self):
        conn = self.provisioner_connect()
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
            conn.close()

    def inspect_account(self, name):
        check_account(name)
        with self.control() as cursor:
            rows = cursor.execute("SHOW USERS").fetchall()
            columns = [column[0].lower() for column in cursor.description]
            user = next(
                (
                    dict(zip(columns, row, strict=True))
                    for row in rows
                    if str(row[columns.index("name")]).upper() == self._user(name)
                ),
                None,
            )
            if user is None:
                return None
            if self.names.suffix:
                # Setting the environment's authentication policy on an existing user would
                # change how its owner signs in.
                raise ValueError(
                    f"Snowflake user {self._user(name)} already exists; in a shared account "
                    "the MCP does not adopt a user, so drop it or choose another warehouse name"
                )
            rows = cursor.execute(f"SHOW GRANTS TO USER {quote(name)}").fetchall()
            columns = [column[0].lower() for column in cursor.description]
            roles = tuple(str(row[columns.index("role")]) for row in rows)
            return Account(
                name,
                roles,
                str(user.get("last_success_login")) if user.get("last_success_login") else None,
                can_adopt=True,
            )

    def create_account(self, name, kind):
        check_account(name, kind)
        user_type = "SERVICE" if kind == "service" else "PERSON"
        with self.control() as cursor:
            cursor.execute(f"CREATE USER {quote(self._user(name))} TYPE = {user_type}")
            if self.names.authentication_policy:
                # The user signs in only through this environment's own integration.
                cursor.execute(
                    f"ALTER USER {quote(self._user(name))} SET AUTHENTICATION POLICY "
                    + self._policy()
                )

    def source_exists(self, table):
        def name(part, default):
            identifier = table.args.get(part)
            if identifier is None:
                return default
            return identifier.name if identifier.args.get("quoted") else identifier.name.upper()

        if name("catalog", self.database) != self.database:
            return False
        with self.control() as cursor:
            return bool(
                cursor.execute(
                    f"CALL {self._access()}.SOURCE_EXISTS(%s,%s)",
                    (name("db", "PUBLIC"), name("this", "")),
                ).fetchone()[0]
            )

    def create_group(self, role):
        check_account(role)
        with self.control() as cursor:
            cursor.execute(f"CREATE ROLE {quote(self._role(role))}")

    def drop_account(self, name):
        check_account(name)
        with self.control() as cursor:
            cursor.execute(f"DROP USER {quote(self._user(name))}")

    def open_membership(self, name):
        check_account(name)

    def close_membership(self, name):
        check_account(name)

    def cleanup_sessions(self):
        pass

    def connect_principal(self, principal, roles, expires_at):
        check_account(principal["warehouse_name"])
        roles = {self._role(r) for r in roles}
        if not roles:
            raise ValueError("principal has no warehouse group role")
        user = self._user(principal["warehouse_name"])
        token = self.mint_token(subject=user, roles=roles, expires_at=expires_at)
        connection = self.query_connect(
            account=self.account,
            user=user,
            authenticator="oauth",
            token=token,
            role=sorted(roles)[0],
            database=self.database,
            warehouse=self.warehouse,
            paramstyle="qmark",
            session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": 30},
        )
        # A single-role token's session activates no secondary roles, and refuses to set any.
        if len(roles) == 1:
            return connection
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("USE SECONDARY ROLES " + ", ".join(quote(r) for r in sorted(roles)))
            finally:
                cursor.close()
            return connection
        except BaseException:
            connection.close()
            raise

    def membership(self, role, name, grant=True):
        check_account(name)
        verb, direction = ("GRANT", "TO") if grant else ("REVOKE", "FROM")
        with self.control() as cursor:
            cursor.execute(
                f"{verb} ROLE {quote(self._role(role))} {direction} USER {quote(self._user(name))}"
            )

    def dataset_grant(self, role, source, grant=True):
        parts = source.split(".")
        if len(parts) not in (2, 3):
            raise ValueError("dataset grant requires a qualified table source")
        if len(parts) == 2:
            parts.insert(0, self.database)
        if parts[0].upper() != self.database.upper():
            raise ValueError("dataset must belong to the configured database")
        self._grant(role, parts[1], parts[2], grant)

    def schema_grant(self, role, schema, grant=True):
        self._grant(role, schema, None, grant)

    def _grant(self, role, schema, relation, grant):
        if role != self.DEFAULT_ROLE:
            check_account(role)
        with self.control() as cursor:
            cursor.execute(
                f"CALL {self._access()}.READ_GRANT(%s,%s,%s,%s)",
                (self._role(role), schema.upper(), relation.upper() if relation else None, grant),
            )

    def _access(self):
        return f"{quote(self.database)}.{quote(self.names.access_schema)}"

    def check_scope(self, registry):
        provisioner = quote(self.names.provisioner_role)
        access = self.database.upper() + "." + self.names.access_schema.upper()
        policy = self.names.authentication_policy
        registry = registry if isinstance(registry, dict) else {}

        def registered(name, kinds):
            key = self._registry_name(name)
            return key is not None and registry.get(key, {}).get("kind") in kinds

        with self.control() as cursor:
            rows = cursor.execute(f"SHOW GRANTS TO ROLE {provisioner}").fetchall()
            columns = [c[0].lower() for c in cursor.description]
            for row in rows:
                grant = dict(zip(columns, row, strict=True))
                privilege, kind, name = grant["privilege"], grant["granted_on"], grant["name"]
                valid = (
                    (kind == "ACCOUNT" and privilege in {"CREATE USER", "CREATE ROLE"})
                    or (
                        kind == "ROLE"
                        and (
                            name.upper() == self.names.analyst_role.upper()
                            or registered(name, {"group"})
                        )
                        and privilege in {"OWNERSHIP", "USAGE"}
                    )
                    or (
                        kind == "USER"
                        and privilege == "OWNERSHIP"
                        and registered(name, {"human", "service"})
                    )
                    or (
                        kind == "AUTHENTICATION_POLICY"
                        and privilege == "APPLY"
                        and policy is not None
                        and name.upper() == f"{access}.{policy}"
                    )
                    or (
                        kind in {"TABLE", "VIEW"}
                        and privilege == "SELECT"
                        and str(grant.get("grant_option")).lower() == "true"
                        and len(name.split(".")) == 3
                        and name.split(".")[0].upper() == self.database.upper()
                        and name.split(".")[1].lower() in {"core", "marts"}
                    )
                    or (
                        kind == "SCHEMA"
                        and privilege == "USAGE"
                        and name.upper()
                        in {self.database.upper() + "." + schema for schema in ("CORE", "MARTS")}
                        | {access}
                    )
                    or (
                        kind == "PROCEDURE"
                        and privilege == "USAGE"
                        and str(grant.get("grant_option")).lower() == "false"
                        and name.upper().replace(" ", "")
                        in {
                            access + "." + signature
                            for signature in (
                                "READ_GRANT(VARCHAR,VARCHAR,VARCHAR,BOOLEAN)",
                                "SOURCE_EXISTS(VARCHAR,VARCHAR)",
                            )
                        }
                    )
                    or (
                        kind in {"DATABASE", "WAREHOUSE"}
                        and privilege == "USAGE"
                        and name.upper() in {self.database.upper(), self.warehouse.upper()}
                    )
                )
                if not valid:
                    raise ValueError(f"provisioner has unexpected grant {privilege} on {name}")
            rows = cursor.execute(f"SHOW FUTURE GRANTS TO ROLE {provisioner}").fetchall()
            columns = [c[0].lower() for c in cursor.description]
            for row in rows:
                grant = dict(zip(columns, row, strict=True))
                name = grant["name"]
                parts = name.upper().split(".")
                if (
                    grant.get("grant_on", grant.get("granted_on")) not in {"TABLE", "VIEW"}
                    or grant["privilege"] != "SELECT"
                    or str(grant.get("grant_option")).lower() != "true"
                    or len(parts) != 3
                    or parts[0] != self.database.upper()
                    or parts[1] not in {"CORE", "MARTS"}
                ):
                    raise ValueError(f"provisioner has unexpected future grant on {name}")

    def drop_group(self, role):
        check_account(role)
        with self.control() as cursor:
            cursor.execute(f"DROP ROLE {quote(self._role(role))}")
