"""Snowflake users keep their own credentials; MCP sessions use External OAuth."""

import json
import re
from contextlib import contextmanager

from vecten_mcp.warehouse.base import Account, RoleExists, check_account


def quote(name):
    return '"' + name.upper().replace('"', '""') + '"'


# What READ_GRANT answers the version call with when it predates the version, or when no
# such procedure is installed.
OLD_INSTALL = re.compile(r"not a bounded reader role|does not exist|unknown user-defined", re.I)


def name_parts(name):
    """The parts of a dotted name as SHOW GRANTS prints it. An unquoted part, or a quoted one
    naming the same upper-case identifier, is in lower case as the state spells it; any other
    quoted part keeps its quotes, so it never matches a schema or relation of another case."""
    parts = []
    for part in re.findall(r'"(?:[^"]|"")*"|[^.]+', name):
        inner = part[1:-1].replace('""', '"') if part.startswith('"') else part
        plain = not part.startswith('"') or re.fullmatch(r"[A-Z_][A-Z0-9_$]*", inner)
        parts.append(inner.lower() if plain else part)
    return parts


class SnowflakeAdapter:
    def __init__(
        self, provisioner_connect, query_connect, mint_token, *, account, database, warehouse
    ):
        self.provisioner_connect, self.query_connect = provisioner_connect, query_connect
        self.mint_token = mint_token
        self.account, self.database, self.warehouse = account, database, warehouse

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
        from snowflake.connector.errors import ProgrammingError

        check_account(role)
        with self.control() as cursor:
            try:
                cursor.execute(f"CREATE ROLE {quote(role)}")
            except ProgrammingError as exc:
                if "already exists" in str(exc.msg):
                    raise RoleExists(role) from None
                raise

    def bind_statement(self, bound, role, grant=True):
        """What an administrator runs to nest `bound` under the group's role: PROVISIONER
        neither owns nor holds `bound`, so it cannot grant it."""
        if grant:
            return f"GRANT ROLE {quote(bound)} TO ROLE {quote(role)};"
        return f"REVOKE ROLE {quote(bound)} FROM ROLE {quote(role)};"

    def review_statement(self, bound):
        return f"SHOW GRANTS TO ROLE {quote(bound)};"

    def holds_role(self, role, bound):
        check_account(role)
        with self.control() as cursor:
            rows = cursor.execute(f"SHOW GRANTS TO ROLE {quote(role)}").fetchall()
            columns = [c[0].lower() for c in cursor.description]
        for row in rows:
            grant = dict(zip(columns, row, strict=True))
            if grant["granted_on"] == "ROLE" and grant["name"].upper() == bound.upper():
                return True
        return False

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
        return self._grant(role, parts[1], parts[2], grant)

    def schema_grant(self, role, schema, grant=True):
        return self._grant(role, schema, None, grant)

    def _grant(self, role, schema, relation, grant):
        """`<shape> <kind>: <warehouse message>` for each relation kind READ_GRANT reports the
        account refused; the other kinds were applied."""
        if role != "analyst":
            check_account(role)
        with self.control() as cursor:
            row = cursor.execute(
                f'CALL {quote(self.database)}."VECTEN_ACCESS".READ_GRANT(%s,%s,%s,%s)',
                (role.upper(), schema.upper(), relation.upper() if relation else None, grant),
            ).fetchone()
        answer = str(row[0]) if row else ""
        if not answer.startswith("refused: "):
            return []
        return [f"{r['kind']}: {r['error']}" for r in json.loads(answer.removeprefix("refused: "))]

    def access_version(self):
        """The installed procedures' version: READ_GRANT reports it when every argument is
        NULL, and an install from before that refuses the call."""
        from snowflake.connector.errors import ProgrammingError

        with self.control() as cursor:
            try:
                answer = cursor.execute(
                    f'CALL {quote(self.database)}."VECTEN_ACCESS".READ_GRANT(NULL,NULL,NULL,NULL)'
                ).fetchone()[0]
            except ProgrammingError as exc:
                if OLD_INSTALL.search(str(exc.msg)):
                    return 1
                raise
        match = re.fullmatch(r"vecten-access (\d+)", str(answer))
        return int(match.group(1)) if match else 1

    def read_grants(self, roles):
        """`(role, kind, schema, relation, object kind)` for every USAGE, SELECT and future
        SELECT the roles hold in this database, read through READ_GRANTS; a database-level
        future grant has no schema."""
        for role in roles:
            if role != "analyst":
                check_account(role)
        held = set()
        with self.control() as cursor:
            for role in sorted(roles):
                answer = cursor.execute(
                    f'CALL {quote(self.database)}."VECTEN_ACCESS".READ_GRANTS(%s)',
                    (role.upper(),),
                ).fetchone()[0]
                answer = answer if isinstance(answer, dict) else json.loads(answer)
                for grant in answer.get("grants", []):
                    parts = name_parts(grant["name"])
                    kind = str(grant["granted_on"]).lower()
                    if parts[0] != self.database.lower():
                        continue
                    if grant["privilege"] == "USAGE" and kind == "schema":
                        held.add((role, "usage", parts[1], None, "schema"))
                    elif grant["privilege"] == "SELECT" and len(parts) == 3:
                        held.add((role, "select", parts[1], parts[2], kind))
                # A database-level future grant (two parts) reaches every schema that holds
                # no schema-level one; it has no schema of its own.
                for grant in answer.get("future", []):
                    parts = name_parts(grant["name"])
                    kind = str(grant["grant_on"]).lower()
                    if parts[0] == self.database.lower() and len(parts) in (2, 3):
                        schema = parts[1] if len(parts) == 3 else None
                        held.add((role, "future", schema, None, kind))
        return held

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
                                "READ_GRANTS(VARCHAR)",
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
