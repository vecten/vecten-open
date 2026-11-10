"""Transactional MCP state; bearer material is hashed by its owning service."""

from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.pool import StaticPool

from vecten_mcp.identity import Identity

metadata = MetaData()
principals = Table(
    "principals",
    metadata,
    Column("id", String, primary_key=True),
    Column("issuer", String),
    Column("subject", String),
    Column("email", String, nullable=False, unique=True),
    Column("name", String, nullable=False),
    Column("warehouse_name", String(63), nullable=False, unique=True),
    Column("kind", String, nullable=False),
    Column("admin", Boolean, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("provisioned", Boolean, nullable=False),
    Column("adopted", Boolean, nullable=False),
    UniqueConstraint("issuer", "subject"),
)
groups = Table(
    "groups",
    metadata,
    Column("name", String, primary_key=True),
    Column("warehouse_role", String, nullable=False, unique=True),
)
memberships = Table(
    "memberships",
    metadata,
    Column(
        "principal_id", String, ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("group_name", String, ForeignKey("groups.name", ondelete="CASCADE"), primary_key=True),
)
records = Table(
    "records",
    metadata,
    Column("kind", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("body", JSON, nullable=False),
    Column("expires_at", Float),
)
access_events = Table(
    "access_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("at", Float, primary_key=True),
    Column("principal_id", String),
    Column("operation", String, nullable=False),
    Column("subject", String),
    Column("outcome", String, nullable=False),
)
audit_log = Table(
    "audit_log",
    metadata,
    Column("id", String, primary_key=True),
    Column("at", Float, primary_key=True),
    Column("body", JSON, nullable=False),
)
schema_version = Table("schema_version", metadata, Column("version", Integer, primary_key=True))


class StateStore:
    def __init__(self, url: str):
        options = {"pool_pre_ping": True}
        if str(url).startswith("sqlite:"):
            options.update(connect_args={"check_same_thread": False, "timeout": 30})
            if url in {"sqlite://", "sqlite:///:memory:"}:
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        if str(url).startswith("sqlite:"):
            event.listen(self.engine, "connect", lambda db, _: db.execute("PRAGMA foreign_keys=ON"))
        if self.engine.dialect.name == "postgresql":
            self.check_audit_scope()
            metadata.create_all(
                self.engine,
                tables=[
                    table
                    for table in metadata.sorted_tables
                    if table.name not in {"audit_log", "access_events"}
                ],
            )
        else:
            metadata.create_all(self.engine)
        with self.transaction() as conn:
            if conn.execute(select(schema_version)).first() is None:
                conn.execute(insert(schema_version).values(version=1))
            if conn.execute(select(groups).where(groups.c.name == "default")).first() is None:
                conn.execute(insert(groups).values(name="default", warehouse_role="analyst"))

    def check_audit_scope(self):
        if self.engine.dialect.name != "postgresql":
            return
        with self.engine.connect() as conn:
            role = conn.execute(
                text(
                    "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls, "
                    "EXISTS (SELECT FROM pg_database WHERE datname=current_database() "
                    "AND pg_has_role(current_user, datdba, 'USAGE')) "
                    "FROM pg_roles WHERE rolname=current_user"
                )
            ).one()
            owner = conn.execute(
                text(
                    "SELECT oid FROM pg_roles WHERE rolname='mcp_state_maintenance' "
                    "AND NOT rolcanlogin"
                )
            ).scalar_one_or_none()
            if owner is None or any(role):
                raise ValueError(
                    "state audit requires administrator setup and a bounded runtime role"
                )
            elevated = conn.execute(
                text(
                    "SELECT pg_has_role(current_user, :owner, 'SET') OR "
                    "pg_has_role(current_user, :owner, 'USAGE')"
                ),
                {"owner": owner},
            ).scalar_one()
            tables = conn.execute(
                text(
                    "SELECT c.relname, c.relowner=:owner AND c.relkind='p' "
                    "AND has_table_privilege(current_user,c.oid,'INSERT') "
                    "AND has_table_privilege(current_user,c.oid,'SELECT') "
                    "AND NOT has_table_privilege(current_user,c.oid,"
                    "'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND c.relname IN ('audit_log','access_events')"
                ),
                {"owner": owner},
            ).all()
            if elevated or len(tables) != 2 or not all(row[1] for row in tables):
                raise ValueError(
                    "state audit tables must be append-only; rerun administrator setup"
                )
            unsafe_children = conn.execute(
                text(
                    "SELECT EXISTS (SELECT FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                    "WHERE i.inhparent IN ('public.audit_log'::regclass,"
                    "'public.access_events'::regclass) "
                    "AND (c.relowner<>:owner OR has_table_privilege(current_user,c.oid,"
                    "'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')))"
                ),
                {"owner": owner},
            ).scalar_one()
            unsafe_helper = conn.execute(
                text(
                    "SELECT n.nspowner<>:owner OR "
                    "has_schema_privilege(current_user,n.oid,'CREATE') "
                    "FROM pg_namespace n WHERE n.nspname='vecten_audit'"
                ),
                {"owner": owner},
            ).scalar_one_or_none()
            if unsafe_children or unsafe_helper is not False:
                raise ValueError("state audit partitions and maintenance schema must be protected")

    def maintain_audit(self, retention_days=90):
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("invalid audit retention")
        if self.engine.dialect.name == "postgresql":
            with self.transaction() as conn:
                conn.execute(text("SELECT vecten_audit.maintain(:days)"), {"days": retention_days})

    @contextmanager
    def transaction(self):
        with self.engine.begin() as conn:
            yield conn

    def close(self):
        self.engine.dispose()

    def create_principal(
        self,
        identity: Identity,
        warehouse_name: str,
        *,
        kind="human",
        admin=False,
        provisioned=False,
        adopted=False,
        conn=None,
    ):
        return self._create(
            identity.issuer,
            identity.subject,
            identity.email,
            identity.name,
            warehouse_name,
            kind,
            admin,
            provisioned,
            adopted,
            conn,
        )

    def invite(
        self,
        email,
        name,
        warehouse_name,
        *,
        admin=False,
        provisioned=False,
        adopted=False,
        conn=None,
    ):
        return self._create(
            None,
            None,
            email.lower(),
            name,
            warehouse_name,
            "human",
            admin,
            provisioned,
            adopted,
            conn,
        )

    def _create(
        self, issuer, subject, email, name, warehouse_name, kind, admin, provisioned, adopted, conn
    ):
        if conn is None:
            with self.transaction() as connection:
                return self._create(
                    issuer,
                    subject,
                    email,
                    name,
                    warehouse_name,
                    kind,
                    admin,
                    provisioned,
                    adopted,
                    connection,
                )
        row = dict(
            id=uuid4().hex,
            issuer=issuer,
            subject=subject,
            email=email,
            name=name,
            warehouse_name=warehouse_name,
            kind=kind,
            admin=admin,
            enabled=True,
            provisioned=provisioned,
            adopted=adopted,
        )
        conn.execute(insert(principals).values(**row))
        conn.execute(insert(memberships).values(principal_id=row["id"], group_name="default"))
        return row

    def principal(self, principal_id, *, conn=None):
        if conn is None:
            with self.transaction() as connection:
                return self.principal(principal_id, conn=connection)
        row = (
            conn.execute(select(principals).where(principals.c.id == principal_id))
            .mappings()
            .first()
        )
        return dict(row) if row else None

    def principals(self):
        with self.transaction() as conn:
            return [dict(row) for row in conn.execute(select(principals)).mappings()]

    def by_email(self, email):
        with self.transaction() as conn:
            row = (
                conn.execute(select(principals).where(principals.c.email == email.lower()))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def bind_identity(self, identity: Identity):
        with self.transaction() as conn:
            row = (
                conn.execute(
                    select(principals).where(principals.c.email == identity.email).with_for_update()
                )
                .mappings()
                .first()
            )
            if not row or not row["enabled"] or row["kind"] != "human":
                raise ValueError("identity has not been invited or is disabled")
            if row["issuer"] is not None and (row["issuer"], row["subject"]) != (
                identity.issuer,
                identity.subject,
            ):
                raise ValueError("email is already bound to another login identity")
            conn.execute(
                update(principals)
                .where(principals.c.id == row["id"])
                .values(issuer=identity.issuer, subject=identity.subject)
            )
            return self.principal(row["id"], conn=conn)

    def update_principal(self, principal_id, *, conn=None, **values):
        allowed = {"admin", "enabled", "provisioned", "adopted", "name"}
        if set(values) - allowed:
            raise ValueError("principal identity bindings are immutable")
        if conn is None:
            with self.transaction() as connection:
                return self.update_principal(principal_id, conn=connection, **values)
        conn.execute(update(principals).where(principals.c.id == principal_id).values(**values))
        return self.principal(principal_id, conn=conn)

    def group(self, name):
        with self.transaction() as conn:
            row = conn.execute(select(groups).where(groups.c.name == name)).mappings().first()
            return dict(row) if row else None

    def groups_for(self, principal_id):
        with self.transaction() as conn:
            return set(
                conn.execute(
                    select(memberships.c.group_name).where(
                        memberships.c.principal_id == principal_id
                    )
                ).scalars()
            )

    def put(self, kind, key, body, *, expires_at=None, conn=None):
        if conn is None:
            with self.transaction() as connection:
                return self.put(kind, key, body, expires_at=expires_at, conn=connection)
        values = dict(kind=kind, key=key, body=body, expires_at=expires_at)
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as upsert
        else:
            from sqlalchemy.dialects.sqlite import insert as upsert
        conn.execute(
            upsert(records)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["kind", "key"], set_={"body": body, "expires_at": expires_at}
            )
        )

    def get(self, kind, key, *, now=None, conn=None, lock=False):
        if conn is None:
            with self.transaction() as connection:
                return self.get(kind, key, now=now, conn=connection, lock=lock)
        stmt = select(records).where(records.c.kind == kind, records.c.key == key)
        if lock:
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).mappings().first()
        if not row or (
            now is not None and row["expires_at"] is not None and row["expires_at"] <= now
        ):
            return None
        return row["body"]

    def take(self, kind, key, *, now, conn=None):
        if conn is None:
            with self.transaction() as connection:
                return self.take(kind, key, now=now, conn=connection)
        row = conn.execute(
            delete(records)
            .where(records.c.kind == kind, records.c.key == key)
            .returning(records.c.body, records.c.expires_at)
        ).first()
        return row.body if row and (row.expires_at is None or row.expires_at > now) else None

    def remove(self, kind, key, *, conn=None):
        if conn is None:
            with self.transaction() as connection:
                return self.remove(kind, key, conn=connection)
        conn.execute(delete(records).where(records.c.kind == kind, records.c.key == key))

    def scan(self, kind, *, conn=None):
        if conn is None:
            with self.transaction() as connection:
                return self.scan(kind, conn=connection)
        return [
            (row.key, row.body)
            for row in conn.execute(select(records).where(records.c.kind == kind))
        ]

    def prune(self, now):
        with self.transaction() as conn:
            conn.execute(delete(records).where(records.c.expires_at <= now))

    def access_event(self, principal_id, operation, subject, outcome, *, now):
        with self.transaction() as conn:
            conn.execute(
                insert(access_events).values(
                    id=uuid4().hex,
                    at=now,
                    principal_id=principal_id,
                    operation=operation,
                    subject=subject,
                    outcome=outcome,
                )
            )
