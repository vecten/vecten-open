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
                    if str(row[columns.index("name")]).upper() == name.upper()
                ),
                None,
            )
            if user is None:
                return None
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
            cursor.execute(f"CREATE USER {quote(name)} TYPE = {user_type}")

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
                    f'CALL {quote(self.database)}."VECTEN_ACCESS".SOURCE_EXISTS(%s,%s)',
                    (name("db", "PUBLIC"), name("this", "")),
                ).fetchone()[0]
            )

    def create_group(self, role):
        check_account(role)
        with self.control() as cursor:
            cursor.execute(f"CREATE ROLE {quote(role)}")

    def drop_account(self, name):
        check_account(name)
        with self.control() as cursor:
            cursor.execute(f"DROP USER {quote(name)}")

    def open_membership(self, name):
        check_account(name)

    def close_membership(self, name):
        check_account(name)

    def cleanup_sessions(self):
        pass

    def connect_principal(self, principal, roles, expires_at):
        check_account(principal["warehouse_name"])
        roles = {r.upper() for r in roles}
        if not roles:
            raise ValueError("principal has no warehouse group role")
        token = self.mint_token(
            subject=principal["warehouse_name"].upper(), roles=roles, expires_at=expires_at
        )
        connection = self.query_connect(
            account=self.account,
            user=principal["warehouse_name"].upper(),
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
            cursor.execute(f"{verb} ROLE {quote(role)} {direction} USER {quote(name)}")

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
        if role != "analyst":
            check_account(role)
        with self.control() as cursor:
            cursor.execute(
                f'CALL {quote(self.database)}."VECTEN_ACCESS".READ_GRANT(%s,%s,%s,%s)',
                (role.upper(), schema.upper(), relation.upper() if relation else None, grant),
            )

    def check_scope(self, registry):
        with self.control() as cursor:
            rows = cursor.execute("SHOW GRANTS TO ROLE PROVISIONER").fetchall()
            columns = [c[0].lower() for c in cursor.description]
            for row in rows:
                grant = dict(zip(columns, row, strict=True))
                privilege, kind, name = grant["privilege"], grant["granted_on"], grant["name"]
                valid = (
                    (kind == "ACCOUNT" and privilege in {"CREATE USER", "CREATE ROLE"})
                    or (
                        kind == "ROLE"
                        and (
                            name.lower() == "analyst"
                            or (
                                isinstance(registry, dict)
                                and registry.get(name.lower(), {}).get("kind") == "group"
                            )
                        )
                        and privilege in {"OWNERSHIP", "USAGE"}
                    )
                    or (
                        kind == "USER"
                        and privilege == "OWNERSHIP"
                        and isinstance(registry, dict)
                        and registry.get(name.lower(), {}).get("kind") in {"human", "service"}
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
                        in {
                            self.database.upper() + "." + schema
                            for schema in ("CORE", "MARTS", "VECTEN_ACCESS")
                        }
                    )
                    or (
                        kind == "PROCEDURE"
                        and privilege == "USAGE"
                        and str(grant.get("grant_option")).lower() == "false"
                        and name.upper().replace(" ", "")
                        in {
                            self.database.upper() + ".VECTEN_ACCESS." + signature
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
            rows = cursor.execute("SHOW FUTURE GRANTS TO ROLE PROVISIONER").fetchall()
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
            cursor.execute(f"DROP ROLE {quote(role)}")
