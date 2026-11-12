"""Postgres control uses provisioner; query connections use only impersonator."""

from contextlib import contextmanager

from psycopg import sql

from vecten_mcp.warehouse.base import Account, check_account
from vecten_shared.identity_setup import ACCESS_VERSION

READ_GRANTS = """
WITH managed AS (SELECT oid, rolname FROM pg_catalog.pg_roles WHERE rolname = ANY(%s))
SELECT m.rolname, 'usage', n.nspname, NULL, 'schema'
FROM pg_catalog.pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a
JOIN managed m ON m.oid = a.grantee
WHERE a.privilege_type = 'USAGE'
UNION ALL
SELECT m.rolname, 'select', n.nspname, c.relname,
       CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'partitioned table'
                      WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view'
                      ELSE 'foreign table' END
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(c.relacl) a JOIN managed m ON m.oid = a.grantee
WHERE a.privilege_type = 'SELECT' AND c.relkind IN ('r','p','v','m','f')
UNION ALL
SELECT m.rolname, 'future', n.nspname, NULL, 'table'
FROM pg_catalog.pg_default_acl d LEFT JOIN pg_catalog.pg_namespace n
  ON n.oid = d.defaclnamespace
CROSS JOIN LATERAL aclexplode(d.defaclacl) a JOIN managed m ON m.oid = a.grantee
WHERE a.privilege_type = 'SELECT' AND d.defaclobjtype = 'r'
"""


def identifier(name):
    return sql.Identifier(name)


class PostgresAdapter:
    def __init__(self, provisioner_connect, impersonator_connect, *, statement_timeout_ms=30000):
        self.provisioner_connect = provisioner_connect
        self.impersonator_connect = impersonator_connect
        self.statement_timeout_ms = statement_timeout_ms

    @contextmanager
    def control(self):
        conn = self.provisioner_connect()
        try:
            yield conn
        finally:
            conn.close()

    def inspect_account(self, name):
        check_account(name)
        with self.control() as conn:
            if (
                conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
                is None
            ):
                return None
            rows = conn.execute(
                (
                    "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON "
                    "r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member WHERE u.rolname=%s"
                ),
                (name,),
            ).fetchall()
            own = conn.execute(
                (
                    "SELECT m.admin_option, m.inherit_option, m.set_option FROM "
                    "pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN pg_roles u "
                    "ON u.oid=m.member WHERE u.rolname='provisioner' AND r.rolname=%s"
                ),
                (name,),
            ).fetchone()
            permitted = bool(own and own[0] and not own[1] and not own[2])
            adoption = (
                sql.SQL("GRANT {} TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE")
                .format(identifier(name))
                .as_string()
            )
            # PostgreSQL does not persist a role's last login in its catalog.
            return Account(
                name,
                tuple(row[0] for row in rows),
                can_adopt=permitted,
                adoption_sql=None if permitted else adoption,
            )

    def source_exists(self, table):
        def name(part):
            identifier = table.args.get(part)
            if identifier is None:
                return None
            return identifier.name if identifier.args.get("quoted") else identifier.name.lower()

        with self.control() as conn:
            return bool(
                conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE COALESCE(%s, current_database())=current_database() "
                    "AND n.nspname=COALESCE(%s, 'public') AND c.relname=%s "
                    "AND c.relkind IN ('r','p','v','m','f'))",
                    (name("catalog"), name("db"), name("this")),
                ).fetchone()[0]
            )

    def create_account(self, name, kind):
        check_account(name, kind)
        with self.control() as conn:
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(identifier(name)))

    def create_group(self, role):
        check_account(role)
        with self.control() as conn:
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(identifier(role)))

    def drop_account(self, name):
        check_account(name)
        with self.control() as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(identifier(name)))

    def open_membership(self, name):
        check_account(name)
        with self.control() as conn:
            conn.execute(
                sql.SQL("GRANT {} TO {} WITH SET TRUE, INHERIT FALSE").format(
                    identifier(name), identifier("impersonator")
                )
            )

    def close_membership(self, name):
        check_account(name)
        with self.control() as conn:
            conn.execute(
                sql.SQL("REVOKE {} FROM {}").format(identifier(name), identifier("impersonator"))
            )

    def stale_memberships(self):
        with self.control() as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON "
                    "r.oid=m.roleid JOIN pg_roles u ON u.oid=m.member WHERE "
                    "u.rolname='impersonator'"
                ).fetchall()
            }

    def cleanup_sessions(self):
        for name in self.stale_memberships():
            self.close_membership(name)

    def connect_principal(self, principal, roles, expires_at):
        check_account(principal["warehouse_name"])
        conn = self.impersonator_connect()
        try:
            conn.execute(sql.SQL("SET ROLE {}").format(identifier(principal["warehouse_name"])))
            conn.execute(
                "SELECT set_config('application_name', %s, false)", (principal["warehouse_name"],)
            )
            conn.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(self.statement_timeout_ms),),
            )
            conn.execute("SET default_transaction_read_only = on")
            return conn
        except BaseException:
            conn.close()
            raise

    def membership(self, role, name, grant=True):
        check_account(name)
        with self.control() as conn:
            statement = (
                "GRANT {} TO {} WITH INHERIT TRUE, SET TRUE" if grant else "REVOKE {} FROM {}"
            )
            conn.execute(sql.SQL(statement).format(identifier(role), identifier(name)))

    def dataset_grant(self, role, source, grant=True):
        parts = source.split(".")
        if len(parts) not in (2, 3) or any(not x or '"' in x for x in parts):
            raise ValueError("dataset grant requires a qualified table source")
        with self.control() as conn:
            if len(parts) == 3:
                database = conn.execute("SELECT current_database()").fetchone()[0]
                if database != parts[0]:
                    raise ValueError("dataset belongs to another database")
            self._grant(conn, role, parts[-2], grant, parts[-1])

    def schema_grant(self, role, schema, grant=True):
        with self.control() as conn:
            self._grant(conn, role, schema, grant)

    def _grant(self, conn, role, schema, grant, relation=None):
        if role != "analyst":
            check_account(role)
        with conn.transaction():
            if not conn.execute("SELECT to_regclass('vecten_access.helpers')").fetchone()[0]:
                raise ValueError("administrator must install schema access helpers")
            helpers = conn.execute(
                "SELECT function_name, owner_oid FROM vecten_access.helpers "
                "WHERE schema_name=%s ORDER BY function_name",
                (schema,),
            ).fetchall()
            if not helpers:
                raise ValueError("schema has no administrator-approved access helper")
            owners = conn.execute(
                "SELECT r.oid FROM pg_namespace n CROSS JOIN pg_roles r "
                "WHERE n.nspname=%s AND (r.oid=n.nspowner OR "
                "EXISTS (SELECT FROM pg_class c WHERE c.relnamespace=n.oid AND c.relowner=r.oid) "
                "OR EXISTS (SELECT FROM pg_default_acl d WHERE d.defaclnamespace=n.oid "
                "AND d.defaclrole=r.oid) "
                "OR (NOT r.rolsuper AND NOT EXISTS (SELECT FROM pg_roles s "
                "WHERE s.rolname='rds_superuser' AND pg_has_role(r.oid,s.oid,'USAGE')) "
                "AND has_schema_privilege(r.oid,n.oid,'CREATE')))",
                (schema,),
            ).fetchall()
            if not {row[0] for row in owners} <= {row[1] for row in helpers}:
                raise ValueError(
                    "object creators changed; administrator must reinstall access helpers"
                )
            if (
                relation is not None
                and not conn.execute(
                    "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN ('r','p','v','m','f')",
                    (schema, relation),
                ).fetchone()
            ):
                raise ValueError("dataset relation does not exist")
            for function, _ in helpers:
                conn.execute(
                    sql.SQL("SELECT {}(%s,%s,%s)").format(
                        sql.Identifier("vecten_access", function)
                    ),
                    (role, grant, relation),
                )

    def access_version(self):
        # The catalogs are read directly and drift is revoked through the owner helpers,
        # whose behaviour no installed version changes.
        return ACCESS_VERSION

    def read_grants(self, roles):
        """`(role, kind, schema, relation, object kind)` for every USAGE, SELECT and default
        SELECT the roles hold, from the catalogs every role may read; a default privilege set
        for the whole database has no schema."""
        with self.control() as conn:
            rows = conn.execute(READ_GRANTS, (sorted(roles),)).fetchall()
        return {tuple(row) for row in rows}

    def check_scope(self, registry):
        registry = set(registry)
        with self.control() as conn:
            self._check_helpers(conn, registry)
            for name in ("provisioner", "impersonator"):
                row = conn.execute(
                    (
                        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                        "rolbypassrls, rolcanlogin, rolinherit FROM pg_roles WHERE rolname=%s"
                    ),
                    (name,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"missing warehouse role {name}")
                for flag, value in zip(
                    ("SUPERUSER", "CREATEDB", "CREATEROLE", "REPLICATION", "BYPASSRLS"),
                    row[:5],
                    strict=True,
                ):
                    if value and not (name == "provisioner" and flag == "CREATEROLE"):
                        raise ValueError(f"{name} has unexpected {flag}")
                if (
                    not row[5]
                    or (name == "provisioner" and not row[2])
                    or (name == "impersonator" and row[6])
                ):
                    raise ValueError(f"{name} has invalid LOGIN/CREATEROLE/INHERIT attributes")
                memberships = conn.execute(
                    (
                        "SELECT r.rolname, m.admin_option, m.inherit_option, m.set_option "
                        "FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid JOIN "
                        "pg_roles u ON u.oid=m.member WHERE u.rolname=%s"
                    ),
                    (name,),
                ).fetchall()
                for role, admin, inherit, setting in memberships:
                    allowed = registry | {"analyst"}
                    if (
                        role not in allowed
                        or inherit
                        or (name == "impersonator" and (admin or not setting))
                        or (name == "provisioner" and (not admin or setting))
                    ):
                        raise ValueError(f"{name} has unexpected membership {role}")
                privileges = conn.execute(
                    (
                        "SELECT n.nspname,c.relname,a.privilege_type,a.is_grantable FROM "
                        "pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN "
                        "LATERAL aclexplode(c.relacl) a JOIN pg_roles r ON r.oid=a.grantee "
                        "WHERE r.rolname=%s"
                    ),
                    (name,),
                ).fetchall()
                for schema, table, privilege, grantable in privileges:
                    if name == "provisioner" and (schema, table, privilege, grantable) == (
                        "vecten_access",
                        "helpers",
                        "SELECT",
                        False,
                    ):
                        continue
                    if (
                        name == "impersonator"
                        or privilege != "SELECT"
                        or not grantable
                        or schema not in {"core", "marts"}
                    ):
                        raise ValueError(
                            f"{name} has unexpected privilege {schema}.{table}:{privilege}"
                        )
                future = conn.execute(
                    "SELECT n.nspname,d.defaclobjtype,a.privilege_type,a.is_grantable "
                    "FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace "
                    "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
                    "JOIN pg_roles r ON r.oid=a.grantee WHERE r.rolname=%s",
                    (name,),
                ).fetchall()
                for schema, kind, privilege, grantable in future:
                    if (
                        name != "provisioner"
                        or schema not in {"core", "marts"}
                        or kind != "r"
                        or privilege != "SELECT"
                        or not grantable
                    ):
                        raise ValueError(
                            f"{name} has unexpected future privilege {schema}:{privilege}"
                        )
                schemas = conn.execute(
                    (
                        "SELECT n.nspname,a.privilege_type FROM pg_namespace n CROSS JOIN "
                        "LATERAL aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid=a.grantee "
                        "WHERE r.rolname=%s"
                    ),
                    (name,),
                ).fetchall()
                for schema, privilege in schemas:
                    if (
                        name == "impersonator"
                        or schema not in {"core", "marts", "vecten_access"}
                        or privilege != "USAGE"
                    ):
                        raise ValueError(
                            f"{name} has unexpected schema privilege {schema}:{privilege}"
                        )
                databases = conn.execute(
                    "SELECT d.datname,a.privilege_type FROM pg_database d CROSS JOIN "
                    "LATERAL aclexplode(d.datacl) a JOIN pg_roles r ON r.oid=a.grantee "
                    "WHERE r.rolname=%s",
                    (name,),
                ).fetchall()
                current_database = conn.execute("SELECT current_database()").fetchone()[0]
                for database, privilege in databases:
                    if database != current_database or privilege != "CONNECT":
                        raise ValueError(
                            f"{name} has unexpected database grant {database}:{privilege}"
                        )
                owned = conn.execute(
                    "SELECT 'schema', nspname FROM pg_namespace n "
                    "JOIN pg_roles r ON r.oid=n.nspowner "
                    "WHERE r.rolname=%s UNION ALL "
                    "SELECT 'database', datname FROM pg_database d "
                    "JOIN pg_roles r ON r.oid=d.datdba "
                    "WHERE r.rolname=%s UNION ALL "
                    "SELECT 'function', proname FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
                    "WHERE r.rolname=%s LIMIT 1",
                    (name, name, name),
                ).fetchone()
                if owned:
                    raise ValueError(f"{name} owns unexpected {owned[0]} {owned[1]}")
                function_grants = conn.execute(
                    "SELECT n.nspname,p.proname,p.proowner,p.prosecdef,p.proconfig "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "CROSS JOIN LATERAL aclexplode(p.proacl) a "
                    "JOIN pg_roles r ON r.oid=a.grantee WHERE r.rolname=%s",
                    (name,),
                ).fetchall()
                for schema, function, owner, definer, config in function_grants:
                    if (
                        name != "provisioner"
                        or schema != "vecten_access"
                        or not definer
                        or config != ["search_path=pg_catalog, pg_temp"]
                        or not conn.execute(
                            "SELECT 1 FROM vecten_access.helpers "
                            "WHERE function_name=%s AND owner_oid=%s",
                            (function, owner),
                        ).fetchone()
                    ):
                        raise ValueError(f"{name} has unexpected function grant {function}")
                ownership = conn.execute(
                    (
                        "SELECT c.relname FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner"
                        " WHERE r.rolname=%s LIMIT 1"
                    ),
                    (name,),
                ).fetchone()
                if ownership:
                    raise ValueError(f"{name} owns unexpected object {ownership[0]}")

    def _check_helpers(self, conn, registry):
        if not conn.execute("SELECT to_regclass('vecten_access.helpers')").fetchone()[0]:
            return
        owners = conn.execute(
            "SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner "
            "WHERE n.nspname='vecten_access' UNION "
            "SELECT r.rolname FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner "
            "WHERE c.oid='vecten_access.helpers'::regclass UNION "
            "SELECT r.rolname FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
            "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='vecten_access'"
        ).fetchall()
        if {row[0] for row in owners} & (registry | {"analyst", "provisioner", "impersonator"}):
            raise ValueError("access helper has a managed owner")
        unsafe = conn.execute(
            "SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) a "
            "WHERE n.nspname='vecten_access' AND a.privilege_type='CREATE' "
            "AND a.grantee<>n.nspowner UNION ALL "
            "SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a "
            "WHERE c.oid='vecten_access.helpers'::regclass AND a.privilege_type<>'SELECT' "
            "AND a.grantee<>c.relowner LIMIT 1"
        ).fetchone()
        if unsafe:
            raise ValueError("access helper registry permits unexpected writes")

    def drop_group(self, role):
        self.drop_account(role)
