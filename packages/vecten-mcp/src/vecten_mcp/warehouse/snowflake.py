"""Snowflake users keep their own credentials; MCP sessions use External OAuth."""

import json
import re
from contextlib import contextmanager

from vecten_mcp.warehouse.base import Account, check_account


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
            user = quote(self._user(name))
            cursor.execute(f"CREATE USER {user} TYPE = {user_type}")
            if not self.names.authentication_policy:
                return
            # The user signs in only through this environment's own integration; without the
            # policy any integration in the account would accept a token naming it.
            try:
                cursor.execute(f"ALTER USER {user} SET AUTHENTICATION POLICY {self._policy()}")
            except BaseException as refused:
                try:
                    cursor.execute(f"DROP USER {user}")
                except Exception as kept:
                    raise RuntimeError(
                        f"Snowflake user {user} was created but its authentication policy "
                        f"could not be set ({refused}) and it could not be dropped ({kept}); "
                        f"any integration in the account accepts a token naming it, so have "
                        f"an ACCOUNTADMIN run DROP USER {user}"
                    ) from refused
                raise

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

    def access_version(self):
        """The installed procedures' version: READ_GRANT reports it when every argument is
        NULL, and an install from before that refuses the call."""
        from snowflake.connector.errors import ProgrammingError

        with self.control() as cursor:
            try:
                answer = cursor.execute(
                    f"CALL {self._access()}.READ_GRANT(NULL,NULL,NULL,NULL)"
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
            if role != self.DEFAULT_ROLE:
                check_account(role)
        held = set()
        with self.control() as cursor:
            for role in sorted(roles):
                answer = cursor.execute(
                    f"CALL {self._access()}.READ_GRANTS(%s)",
                    (self._role(role),),
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
        provisioner = quote(self.names.provisioner_role)
        access = self.database.upper() + "." + self.names.access_schema.upper()
        policy = self.names.authentication_policy
        registry = registry if isinstance(registry, dict) else {}

        def registered(name, kinds):
            key = self._registry_name(name)
            return key is not None and registry.get(key, {}).get("kind") in kinds

        owned = set()
        with self.control() as cursor:
            rows = cursor.execute(f"SHOW GRANTS TO ROLE {provisioner}").fetchall()
            columns = [c[0].lower() for c in cursor.description]
            for row in rows:
                grant = dict(zip(columns, row, strict=True))
                if grant["privilege"] == "OWNERSHIP":
                    owned.add((grant["granted_on"], grant["name"].upper()))
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
            if self.names.suffix:
                self._check_holders(cursor, owned)

    def _check_holders(self, cursor, owned):
        """Refuse when the environment's provisioner or analyst role is held by anyone but
        the provisioner's user, the provisioner's role and what it owns, SYSADMIN and the
        deploy role: in a shared account another environment could otherwise hold them, since
        a role's owner and comment can be handed over after the grant."""
        names = self.names
        rows = cursor.execute(f"SHOW DATABASES LIKE '{self.database.upper()}'").fetchall()
        columns = [c[0].lower() for c in cursor.description]
        keepers = {
            str(row[columns.index("owner")]).upper()
            for row in rows
            if str(row[columns.index("name")]).upper() == self.database.upper()
        }
        trusted = {("ROLE", "SYSADMIN"), *(("ROLE", keeper) for keeper in keepers)}
        allowed = {
            names.provisioner_role: trusted | {("USER", names.provisioner_user.upper())},
            names.analyst_role: trusted | owned | {("ROLE", names.provisioner_role.upper())},
        }
        for role, holders in allowed.items():
            rows = cursor.execute(f"SHOW GRANTS OF ROLE {quote(role)}").fetchall()
            columns = [c[0].lower() for c in cursor.description]
            for row in rows:
                grant = dict(zip(columns, row, strict=True))
                holder = (grant["granted_to"], str(grant["grantee_name"]).upper())
                if holder not in holders:
                    raise ValueError(
                        f"{role} is held by {holder[0].lower()} {holder[1]}, outside this "
                        "environment"
                    )

    def drop_group(self, role):
        check_account(role)
        with self.control() as cursor:
            cursor.execute(f"DROP ROLE {quote(self._role(role))}")
