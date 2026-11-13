"""Desired grants are persisted only after their warehouse operation succeeds."""

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from threading import RLock

from sqlalchemy import delete, insert, select

from vecten_mcp.apps import APP_PRINCIPALS
from vecten_mcp.failures import describe_error
from vecten_mcp.identity import RESERVED, warehouse_name
from vecten_mcp.state import groups, memberships
from vecten_mcp.warehouse.base import RoleExists
from vecten_shared.identity_setup import ACCESS_VERSION

logger = logging.getLogger(__name__)
# Records kind: app principal id -> {"roles": every warehouse role it may hold, "failed":
# the last sync failure until a sync succeeds}.
APP_ROLES = "app_roles"

CONVENTION_SCHEMAS = frozenset({"core", "marts", "staging", "snapshots"})
PRIVILEGES = {"usage": "USAGE", "select": "SELECT", "future": "FUTURE SELECT"}
OUTDATED_FIX = (
    "An ACCOUNTADMIN reruns `vecten mcp provisioner-script --env <env>` and confirms it with "
    "--confirm-run: until then area grants wait, and read grants the state does not hold "
    "are neither listed nor revoked."
)


# `p_` is kept for one reader role per person; the others name administrators, services and
# Postgres's predefined roles.
RESERVED_ROLE_PREFIXES = ("admin_", "svc_", "pg_", "p_")
SYSTEM_ROLES = frozenset(
    {"accountadmin", "securityadmin", "sysadmin", "useradmin", "orgadmin", "public"}
)
BOUND_ROLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
AREA_SCHEMA = re.compile(r"(core|marts)_([a-z][a-z0-9]*)")
KINDS = ("dataset", "schema", "area")


def area_schemas(area):
    return [f"core_{area}", f"marts_{area}"]


def in_scope(schema, areas=()):
    """The schemas whose read grants the state database owns for every managed role: the
    convention's, every `raw_` schema, a prefix the convention keeps for loaded sources
    and the bound READ_GRANT and the Postgres helpers already accept, and the schemas of the
    declared areas. A schema named like an undeclared area is the client's."""
    area = AREA_SCHEMA.fullmatch(schema)
    return (
        schema in CONVENTION_SCHEMAS
        or schema.startswith("raw_")
        or bool(area and area.group(2) in areas)
    )


class Undeclared(ValueError):
    """A grant naming an area `vecten.toml` does not declare, which stays closed."""


class AccessService:
    def __init__(self, state, adapter, *, invalidate=None, areas=()):
        self.state, self.adapter = state, adapter
        self.invalidate = invalidate or (lambda pid: None)
        self.datasets = {}
        self.areas = frozenset(areas)
        self.lock = RLock()
        # The bound apps whose admitted groups follow these grants (AppScopes), or None.
        self.apps = None

    def admit(self):
        # Under the grant lock, so a computation from older grants never lands after a newer one.
        with self.lock:
            if self.apps is not None:
                self.apps.admit(self.datasets)
                for principal_id, _ in self.state.scan(APP_PRINCIPALS):
                    try:
                        self.sync_app_roles(principal_id)
                    except Exception as exc:
                        # The query path refuses an app principal holding a role beyond its
                        # admitted groups', so a failed revoke reads nothing until retried.
                        logger.error(
                            "app principal %s roles not synced: %s",
                            principal_id,
                            describe_error(exc),
                        )

    def sync_app_roles(self, principal_id):
        """Grants an app principal, in the warehouse, the roles of its groups that its app
        admits and revokes the others, so its inherited roles never exceed what admitted
        viewers may read. APP_ROLES records every role it may hold."""
        principal = self.state.principal(principal_id)
        if principal is None or self.apps is None:
            return
        groups = self.state.groups_for(principal_id)
        admitted = self.apps.query_groups(principal_id, groups)
        if admitted is None:
            return
        wanted = {self.state.group(g)["warehouse_role"] for g in admitted}
        record = self.state.get(APP_ROLES, principal_id)
        held = (
            set(record["roles"])
            if record
            else {self.state.group(g)["warehouse_role"] for g in groups}
        )
        name = principal["warehouse_name"]
        # A role leaves this superset only once its revoke succeeds, and the failure marker
        # only once every grant and revoke has succeeded.
        recorded = held | wanted
        failed = {"failed": record["failed"]} if record and record.get("failed") else {}

        def save(**marker):
            self.state.put(APP_ROLES, principal_id, {"roles": sorted(recorded), **marker})

        save(**failed)
        errors = []
        # Revokes run first and every call is attempted, so a grant that keeps failing never
        # keeps a role outside the admitted groups' in place. The record names a role before its
        # grant is confirmed, so every wanted role is granted again; both warehouses accept a
        # grant the account already holds.
        try:
            for role in sorted(held - wanted):
                try:
                    self.adapter.membership(role, name, False)
                except Exception as exc:
                    errors.append(exc)
                    continue
                recorded.discard(role)
                save(**failed)
            for role in sorted(wanted):
                try:
                    self.adapter.membership(role, name, True)
                except Exception as exc:
                    errors.append(exc)
        finally:
            if wanted != held:
                self.invalidate(principal_id)
        if errors:
            save(failed={"at": time.time(), "error": describe_error(errors[0])})
            raise errors[0]
        if failed:
            save()

    def app_role_health(self):
        """Access-health entries for app principals whose warehouse roles may differ from
        their admitted groups': a role sync that failed, and roles the warehouse reports
        beyond them, such as one an administrator granted by hand."""
        entries = []
        for principal_id, _ in self.state.scan(APP_PRINCIPALS):
            principal = self.state.principal(principal_id)
            marked, app = self.apps.app_of(principal_id)
            if principal is None or app is None:
                continue
            name = principal["warehouse_name"]
            record = self.state.get(APP_ROLES, principal_id) or {}
            failed = record.get("failed")
            if failed:
                entries.append(
                    {
                        "app": app.key,
                        "principal_id": principal_id,
                        "warehouse_name": name,
                        "reason": "role sync failed",
                        "roles": list(record.get("roles", [])),
                        "error": failed.get("error"),
                        "message": (
                            f"the MCP could not set {name}'s warehouse roles to those of the "
                            f"groups {app.key} admits ({failed.get('error')}); until it "
                            "succeeds app queries may miss an admitted group's data, and are "
                            "refused on Postgres while a role to revoke remains. It retries "
                            "at every reconciliation; `vecten reconcile --env <env>` retries now"
                        ),
                    }
                )
            inspect = getattr(self.adapter, "inspect_account", None)
            if inspect is None:
                continue
            try:
                account = inspect(name)
            except Exception as exc:
                logger.warning("app principal %s roles not read: %s", name, describe_error(exc))
                continue
            groups = self.apps.query_groups(principal_id, self.state.groups_for(principal_id))
            allowed = {"public"} | {
                self.state.group(g)["warehouse_role"].casefold() for g in groups or ()
            }
            extra = sorted(
                r for r in (account.roles if account else ()) if r.casefold() not in allowed
            )
            if extra:
                entries.append(
                    {
                        "app": app.key,
                        "principal_id": principal_id,
                        "warehouse_name": name,
                        "reason": "warehouse roles beyond the admitted groups",
                        "roles": extra,
                        "message": (
                            f"{name} holds {', '.join(extra)} in the warehouse, beyond the roles "
                            f"of the groups {app.key} admits; app queries are refused on "
                            "Postgres while it does. A warehouse administrator revokes them "
                            f"(REVOKE <role> FROM {name}); a role the MCP granted is revoked "
                            "again at the next reconciliation"
                        ),
                    }
                )
        return entries

    def held_roles(self, principal_id):
        """The warehouse roles an app principal holds, as recorded, or None for any other."""
        record = self.state.get(APP_ROLES, principal_id)
        return set(record["roles"]) if record else None

    def seed_defaults(self):
        # No setup grants these: the first reconciliation applies them. Recording them once
        # keeps an administrator's later revocation from coming back on restart.
        with self.lock, self.state.transaction() as conn:
            earlier = bool(self.state.get("bootstrap", "default_grants", conn=conn))
            if not self.state.get("bootstrap", "drift_mode", conn=conn):
                # A state an earlier release seeded may sit beside hand-made grants nobody
                # recorded: they are listed until an admin chooses to revoke them.
                if earlier and not self.state.get("settings", "reconcile_drift", conn=conn):
                    self.state.put("settings", "reconcile_drift", {"value": "report"}, conn=conn)
                self.state.put("bootstrap", "drift_mode", {"seeded": True}, conn=conn)
            if earlier:
                return
            for target in ("core", "marts"):
                row = {"group": "default", "kind": "schema", "target": target}
                key = self._key("default", "schema", target)
                self.state.put("grants", key, row, conn=conn)
                self.state.put("applied_grants", key, dict(row, source=target), conn=conn)
            self.state.put("bootstrap", "default_grants", {"seeded": True}, conn=conn)

    def list_groups(self):
        with self.state.transaction() as conn:
            rows = [dict(row) for row in conn.execute(select(groups)).mappings()]
        bindings = dict(self.state.scan("bindings"))
        for row in rows:
            binding = bindings.get(row["name"])
            if binding:
                row["bound_role"] = binding["bound_role"]
                row["binding"] = "confirmed" if binding["confirmed"] else "pending"
        return rows

    def create_group(self, name, role=None):
        warehouse_name(name, set(), override=name)
        role = self._group_role(name, name if role is None else role)
        with self.lock, self.state.transaction() as conn:
            if self.state.group(name):
                raise ValueError("group already exists")
            conn.execute(insert(groups).values(name=name, warehouse_role=role))
            try:
                self.adapter.create_group(role)
            except RoleExists:
                raise ValueError(
                    f"a warehouse role {role} already exists: create the group with another "
                    f"role name (vecten group add {name} --role <role>), or with another role "
                    f"name and then bind {role} to it (vecten group bind {name} {role})"
                ) from None
            self.state.put("provisioner_registry", role, {"kind": "group"}, conn=conn)
        self.admit()
        return {"name": name, "warehouse_role": role}

    def _group_role(self, name, role):
        if not isinstance(role, str):
            raise ValueError("a group's role is a name")
        warehouse_name(role, set(), override=role)
        if role.startswith(RESERVED_ROLE_PREFIXES):
            raise ValueError(
                f"role {role} starts with a reserved prefix ({', '.join(RESERVED_ROLE_PREFIXES)});"
                f" name another with vecten group add {name} --role <role>"
            )
        owner = next((g["name"] for g in self.list_groups() if g["warehouse_role"] == role), None)
        if owner:
            raise ValueError(f"role {role} already belongs to group {owner}")
        if self.state.get("provisioner_registry", role):
            raise ValueError(f"role {role} already belongs to a principal")
        for group, binding in self.state.scan("bindings"):
            if binding["bound_role"].lower() == role.lower():
                raise ValueError(f"role {role} is bound to group {group}")
        return role

    def _managed_roles(self):
        """Every role the server or setup manages, which a bound role may neither be nor
        reach."""
        return (
            {g["warehouse_role"].lower() for g in self.list_groups()}
            | {name.lower() for name, _ in self.state.scan("provisioner_registry")}
            | RESERVED
        )

    def _check_bound(self, bound):
        problem = self.adapter.bound_role_problem(bound, self._managed_roles())
        if problem:
            raise ValueError(f"{bound} cannot be bound: {problem}")

    def bind(self, group, bound):
        """Record a role the client already has as bound to the group, pending until the
        statement it returns has been run; the server never grants, alters or drops it."""
        if group == "default":
            raise ValueError("the default group binds no role: bind one to another group")
        role = self.require_group(group)["warehouse_role"]
        if not isinstance(bound, str) or not BOUND_ROLE.fullmatch(bound):
            raise ValueError("a bound role is an unquoted warehouse identifier")
        lower = bound.lower()
        managed = {g["warehouse_role"].lower() for g in self.list_groups()}
        if (
            lower in RESERVED
            or lower in SYSTEM_ROLES
            or lower.startswith(RESERVED_ROLE_PREFIXES)
            or lower in managed
            or self.state.get("provisioner_registry", lower)
        ):
            raise ValueError(f"{bound} is a role the server manages or a system role")
        with self.lock:
            held = self.state.get("bindings", group)
            if held and held["bound_role"] != bound:
                raise ValueError(
                    f"group {group} already binds {held['bound_role']}: unbind it first"
                )
            if held:
                # Binding the same role again prints its statement; the record, confirmed
                # or not, stays as it is.
                return self._binding(group, role, bound, confirmed=held["confirmed"])
            self._check_bound(bound)
            self.state.put("bindings", group, {"bound_role": bound, "confirmed": False})
        return self._binding(group, role, bound, confirmed=False)

    def _binding(self, group, role, bound, *, confirmed):
        return {
            "group": group,
            "role": role,
            "bound_role": bound,
            "confirmed": confirmed,
            "statement": self.adapter.bind_statement(bound, role),
            "review": self.adapter.review_statement(bound),
        }

    def _bound(self, group):
        binding = self.state.get("bindings", group)
        if not binding:
            raise ValueError(f"group {group} has no bound role")
        return binding

    def confirm_binding(self, group):
        role = self.require_group(group)["warehouse_role"]
        with self.lock:
            bound = self._bound(group)["bound_role"]
            self._check_bound(bound)
            if not self.adapter.holds_role(role, bound):
                statement = self.adapter.bind_statement(bound, role)
                raise ValueError(
                    f"{role} does not hold {bound} yet: an administrator runs {statement}"
                )
            self.state.put("bindings", group, {"bound_role": bound, "confirmed": True})
        return self._binding(group, role, bound, confirmed=True)

    def unbind(self, group):
        """Forget the binding; the returned statement, run by an administrator, revokes it,
        and until it runs reconciliation lists the role as one the group's role should not
        hold."""
        role = self.require_group(group)["warehouse_role"]
        with self.lock:
            bound = self._bound(group)["bound_role"]
            self.state.remove("bindings", group)
        statement = self.adapter.bind_statement(bound, role, grant=False)
        return {"group": group, "bound_role": bound, "statement": statement}

    def delete_group(self, name):
        if name == "default":
            raise ValueError("default group cannot be deleted")
        group = self.require_group(name)
        with self.lock:
            for grant in self.list_grants():
                if grant["group"] == name:
                    self.revoke(name, grant["kind"], grant["target"])
            with self.state.transaction() as conn:
                ids = list(
                    conn.execute(
                        select(memberships.c.principal_id).where(memberships.c.group_name == name)
                    ).scalars()
                )
            for pid in ids:
                self.membership(name, pid, False)
            self.adapter.drop_group(group["warehouse_role"])
            with self.state.transaction() as conn:
                # Dropping the role took every grant it held, revokes refused above included.
                for key, row in list(self.state.scan("applied_grants")):
                    if row["group"] == name:
                        self.state.remove("applied_grants", key, conn=conn)
                conn.execute(delete(groups).where(groups.c.name == name))
                self.state.remove("bindings", name, conn=conn)
                self.state.remove("provisioner_registry", group["warehouse_role"], conn=conn)
                self.state.remove("denied", name, conn=conn)
            self.admit()
        return {"deleted": name}

    def require_group(self, name):
        group = self.state.group(name)
        if not group:
            raise ValueError("unknown group")
        return group

    def membership(self, group, principal_id, grant=True):
        role = self.require_group(group)["warehouse_role"]
        principal = self.state.principal(principal_id)
        if not principal or not principal["provisioned"]:
            raise ValueError("principal is not provisioned")
        marked, app = self.apps.app_of(principal_id) if self.apps else (False, None)
        if marked:
            if grant and (app is None or group not in app.groups):
                where = app.key if app else "an app the running bundle does not bind"
                raise ValueError(
                    f"{principal['warehouse_name']} is the principal of {where}; it joins only "
                    "the groups vecten.toml binds the app to"
                )
            return self._app_membership(group, principal, grant)
        with self.lock, self.state.transaction() as conn:
            self.adapter.membership(role, principal["warehouse_name"], grant)
            query = delete(memberships).where(
                memberships.c.principal_id == principal_id, memberships.c.group_name == group
            )
            conn.execute(query)
            if grant:
                conn.execute(
                    insert(memberships).values(principal_id=principal_id, group_name=group)
                )
        self.invalidate(principal_id)
        return {"principal_id": principal_id, "group": group, "member": grant}

    def _app_membership(self, group, principal, grant):
        """An app principal's membership changes in state; its warehouse roles then follow
        the groups its app admits."""
        principal_id = principal["id"]
        with self.lock:
            if self.state.get(APP_ROLES, principal_id) is None:
                held = {
                    self.state.group(g)["warehouse_role"]
                    for g in self.state.groups_for(principal_id)
                }
                self.state.put(APP_ROLES, principal_id, {"roles": sorted(held)})
            with self.state.transaction() as conn:
                conn.execute(
                    delete(memberships).where(
                        memberships.c.principal_id == principal_id,
                        memberships.c.group_name == group,
                    )
                )
                if grant:
                    conn.execute(
                        insert(memberships).values(principal_id=principal_id, group_name=group)
                    )
            self.sync_app_roles(principal_id)
        self.invalidate(principal_id)
        return {"principal_id": principal_id, "group": group, "member": grant}

    def _key(self, group, kind, target):
        return hashlib.sha256(json.dumps([group, kind, target]).encode()).hexdigest()

    def _source(self, kind, target):
        if kind not in KINDS:
            raise ValueError("a grant's kind is dataset, schema or area")
        if not isinstance(target, str) or not target:
            raise ValueError("a grant's target is a dataset, schema or area name")
        if kind == "area":
            if target not in self.areas:
                raise Undeclared(
                    f"no [areas.{target}] in vecten.toml: declare the area {target} and ship "
                    "before granting it"
                )
            return area_schemas(target)
        if kind == "schema":
            area = AREA_SCHEMA.fullmatch(target)
            if area and area.group(2) not in self.areas:
                raise Undeclared(
                    f"{target} belongs to no declared area: no [areas.{area.group(2)}] in "
                    "vecten.toml"
                )
            if not area and target not in CONVENTION_SCHEMAS and not target.startswith("raw_"):
                raise ValueError(
                    "schema grants are limited to convention schemas and declared areas"
                )
            if not target.replace("_", "").isalnum():
                raise ValueError("invalid schema name")
            return target
        if target not in self.datasets:
            raise ValueError("unknown dataset")
        source = self.datasets[target]
        parts = source.lower().split(".")
        area = AREA_SCHEMA.fullmatch(parts[-2]) if len(parts) > 1 else None
        if area and area.group(2) not in self.areas:
            raise Undeclared(
                f"dataset {target} reads {parts[-2]}, which belongs to no declared area: no "
                f"[areas.{area.group(2)}] in vecten.toml"
            )
        return source

    def _current(self, kind, source):
        """Whether the grant reaches an area schema, which an older READ_GRANT refuses."""
        if kind == "dataset":
            parts = source.lower().split(".")
            schemas = parts[-2:-1] if len(parts) > 1 else []
        else:
            schemas = source if kind == "area" else [source]
        return any(AREA_SCHEMA.fullmatch(schema) for schema in schemas)

    def _outdated(self):
        installed = self.adapter.access_version()
        if installed < ACCESS_VERSION:
            return {"installed": installed, "required": ACCESS_VERSION, "fix": OUTDATED_FIX}
        return None

    def _apply(self, grant, source, allowed, done=None):
        """The relation kinds the warehouse refused a grant, each with its message; the grant
        holds for every other kind. A revoke that leaves anything refused raises, so its
        applied record stays for the next pass. `done` collects the schemas attempted, the
        failing one included."""
        done = [] if done is None else done
        role = self.require_group(grant["group"])["warehouse_role"]
        if grant["kind"] == "dataset":
            refused = list(self.adapter.dataset_grant(role, source, allowed) or [])
        else:
            refused = []
            for schema in source if grant["kind"] == "area" else [source]:
                # Listed before the call: a refusal can follow a part of the grant.
                done.append(schema)
                kinds = self.adapter.schema_grant(role, schema, allowed) or []
                refused += [f"{schema}: {kind}" for kind in kinds]
        if refused and not allowed:
            raise RuntimeError("the warehouse refused part of the revoke: " + "; ".join(refused))
        if refused:
            logger.warning(
                "%s of %s %s for %s left out: %s",
                "Grant" if allowed else "Revoke",
                grant["kind"],
                grant["target"],
                grant["group"],
                "; ".join(refused),
            )
        return refused

    def grant(self, group, kind, target):
        self.require_group(group)
        source = self._source(kind, target)
        if self._current(kind, source) and self._outdated():
            raise ValueError(f"areas need the current access procedures. {OUTDATED_FIX}")
        row = {"group": group, "kind": kind, "target": target}
        key = self._key(group, kind, target)
        with self.lock:
            done = []
            try:
                refused = self._apply(row, source, True, done)
            except Exception:
                # A grant held before this call stays; reconciliation retries it.
                if kind == "area" and done and not self.state.get("applied_grants", key):
                    self._roll_back(row, done)
                raise
            self.state.put("grants", key, row)
            self.state.put("applied_grants", key, dict(row, source=source))
            self.admit()
        return dict(row, refused=refused) if refused else row

    def _roll_back(self, row, schemas):
        """Revoke the schemas of an area grant that failed part-way; what cannot be revoked is
        recorded as applied, so reconciliation keeps revoking it and lists each failure."""
        role = self.require_group(row["group"])["warehouse_role"]
        left = []
        for schema in schemas:
            try:
                if self.adapter.schema_grant(role, schema, False):
                    raise RuntimeError("the warehouse refused part of the revoke")
            except Exception as exc:
                logger.warning("Rolling back %s on %s failed: %s", row, schema, describe_error(exc))
                left.append(schema)
        if left:
            key = self._key(row["group"], row["kind"], row["target"])
            self.state.put("applied_grants", key, dict(row, source=left))

    def revoke(self, group, kind, target):
        self.require_group(group)
        key = self._key(group, kind, target)
        with self.lock:
            row = self.state.get("grants", key)
            applied = self.state.get("applied_grants", key)
            self.state.remove("grants", key)
            if applied:
                # A revoke the warehouse refuses keeps its applied record: the reconciliation
                # below and every later one retry it and list the failure.
                try:
                    self._apply(applied, applied["source"], False)
                    self.state.remove("applied_grants", key)
                except Exception as exc:
                    logger.warning("Revoking %s failed: %s", applied, describe_error(exc))
            reconciliation = self.reconcile()
        result = row or {"group": group, "kind": kind, "target": target}
        if not reconciliation["complete"]:
            result = dict(result, reconciliation=reconciliation)
        return result

    def list_grants(self):
        return [row for _, row in self.state.scan("grants")]

    def reconcile(self):
        failures, blocked, reconciled = [], set(), 0
        report = {
            "added": [],
            "revoked": [],
            "drift": [],
            "database_future": [],
            "missing_bindings": [],
            "unbound_roles": [],
        }

        def failed(key, row, operation, exc):
            blocked.add(key)
            error = exc if isinstance(exc, str) else describe_error(exc)
            failure = dict(row, operation=operation, error=error)
            failures.append(failure)
            logger.warning(
                "Grant %s of %s %s for %s failed: %s",
                operation,
                row["kind"],
                row["target"],
                row["group"],
                failure["error"],
            )

        with self.lock:
            desired = dict(self.state.scan("grants"))
            applied = dict(self.state.scan("applied_grants"))
            for key, row in list(desired.items()):
                if self._undeclared(row):
                    # An area that left vecten.toml closes: its grants are forgotten, so a
                    # later area of the same name starts with none.
                    self.state.remove("grants", key)
                    del desired[key]
                    logger.info("Forgot %s: its area is not declared", row)
            for key, old in applied.items():
                if self._source_of(desired.get(key)) != old["source"]:
                    try:
                        self._apply(old, old["source"], False)
                        self.state.remove("applied_grants", key)
                    except Exception as exc:
                        failed(key, old, "revoke", exc)
            held, outdated = self._converge(desired, report, failures)
            for key, row in desired.items():
                source = self._source_of(row)
                if key in blocked or source is None:
                    continue
                if outdated and self._current(row["kind"], source):
                    failed(key, row, "grant", outdated["fix"])
                    continue
                done = []
                try:
                    role = self.require_group(row["group"])["warehouse_role"]
                    pieces = self._pieces(role, row["kind"], source)
                    absent = key not in applied if held is None else not pieces <= _folded(held)
                    refused = self._apply(row, source, True, done)
                    self.state.put("applied_grants", key, dict(row, source=source))
                    reconciled += 1
                    if refused:
                        failures.append(dict(row, operation="grant", error="; ".join(refused)))
                    if absent:
                        report["added"].append(dict(row))
                        logger.info("Granted %s %s to %s", row["kind"], row["target"], row["group"])
                except Exception as exc:
                    failed(key, row, "grant", exc)
                    if row["kind"] == "area" and done and key not in applied:
                        # The schemas attempted before the failure stay revocable.
                        self.state.put("applied_grants", key, dict(row, source=done))
            report["missing_bindings"], report["unbound_roles"] = self._bindings(failures)
        health = {"checked_at": time.time(), "failures": failures, **report}
        if outdated:
            health["outdated"] = outdated
        self.state.put("access_health", "reconciliation", health)
        self.admit()
        return {"reconciled": reconciled, "complete": not failures, "failures": failures}

    def _bindings(self, failures):
        """Confirmed bindings whose grant a group's role no longer holds, and every role
        nested under a group's role other than its confirmed binding."""
        missing, unbound = [], []
        bindings = dict(self.state.scan("bindings"))
        for row in sorted(self.list_groups(), key=lambda g: g["name"]):
            group, role = row["name"], row["warehouse_role"]
            if group == "default":
                continue
            binding = bindings.get(group) or {}
            bound = binding.get("bound_role") if binding.get("confirmed") else None
            try:
                nested = self.adapter.granted_roles(role)
                held = self.adapter.holds_role(role, bound) if bound else True
            except Exception as exc:
                error = describe_error(exc)
                failures.append(
                    {
                        "group": group,
                        "kind": "binding",
                        "target": bound or "*",
                        "operation": "read",
                        "error": error,
                    }
                )
                logger.warning("Reading the roles granted to %s failed: %s", role, error)
                continue
            if not held:
                missing.append({"group": group, "role": role, "bound_role": bound})
                logger.warning("%s no longer holds its bound role %s", role, bound)
            for name in sorted(nested):
                if bound is None or name.lower() != bound.lower():
                    unbound.append({"group": group, "role": role, "granted_role": name})
                    logger.warning("%s holds %s, which no confirmed binding names", role, name)
        return missing, unbound

    def _undeclared(self, row):
        try:
            self._source(row["kind"], row["target"])
        except Undeclared:
            return True
        except ValueError:
            return False
        return False

    def _source_of(self, row):
        """What a desired grant reaches now, or None when there is no such grant or its
        dataset is not in the model."""
        if row is None or (row["kind"] == "dataset" and row["target"] not in self.datasets):
            return None
        try:
            return self._source(row["kind"], row["target"])
        except Undeclared:
            return None

    def _pieces(self, role, kind, source):
        """The read grants, as `read_grants` names them without the object kind, that one
        desired grant consists of."""
        if kind == "area":
            return set().union(*(self._pieces(role, "schema", schema) for schema in source))
        if kind == "schema":
            return {(role, "usage", source, None), (role, "future", source, None)}
        schema, relation = source.lower().split(".")[-2:]
        return {(role, "usage", schema, None), (role, "select", schema, relation)}

    def _read(self, roles, failures):
        """What each role holds, read one role at a time so one unreadable role leaves the
        others' drift handled, and the roles that could be read."""
        held, readable = set(), set()
        for role in sorted(roles):
            try:
                held |= set(self.adapter.read_grants({role}))
                readable.add(role)
            except Exception as exc:
                failures.append(self._read_failure(exc, roles[role]))
        return held, readable

    def _converge(self, desired, report, failures):
        """Revoke, or list under `report`, the read grants managed roles hold that the state
        does not; returns what they held after it, or None when it could not be read."""
        try:
            outdated = self._outdated()
        except Exception as exc:
            failures.append(self._read_failure(exc, "*"))
            return None, None
        if outdated:
            logger.warning(
                "Access procedures are version %s; %s", outdated["installed"], OUTDATED_FIX
            )
            return None, outdated
        roles = {row["warehouse_role"]: row["name"] for row in self.list_groups()}
        held, readable = self._read(roles, failures)
        if not readable:
            return None, None
        whole, datasets = set(), defaultdict(set)
        for row in desired.values():
            group = self.state.group(row["group"])
            source = self._source_of(row)
            if group is None or source is None:
                continue
            if row["kind"] == "area":
                whole.update((group["warehouse_role"], schema) for schema in source)
            elif row["kind"] == "schema":
                whole.add((group["warehouse_role"], source))
            else:
                schema, relation = source.lower().split(".")[-2:]
                datasets[(group["warehouse_role"], schema)].add(relation)
        places = defaultdict(list)
        for grant in sorted(held, key=lambda g: tuple(map(str, g))):
            role, kind, schema, relation, _ = grant
            if role not in roles:
                continue
            if schema is None:
                report["database_future"].append(_entry(roles, grant, place=False))
                continue
            if not in_scope(schema, self.areas) or (role, schema) in whole:
                continue
            allowed = datasets.get((role, schema), set())
            if (kind == "usage" and allowed) or (kind == "select" and relation.lower() in allowed):
                continue
            places[(role, schema)].append(grant)
        mode = (self.state.get("settings", "reconcile_drift") or {}).get("value", "revoke")
        if mode == "report":
            for grants in places.values():
                for grant in grants:
                    report["drift"].append(_entry(roles, grant))
                    logger.info("Drift: %s holds %s", grant[0], _named(_entry(roles, grant)))
            return held, None
        failed, touched = set(), set()

        def failure(role, target, exc):
            failed.add((role, target))
            failures.append(
                {
                    "group": roles[role],
                    "kind": "drift",
                    "target": target,
                    "operation": "revoke",
                    "error": exc if isinstance(exc, str) else describe_error(exc),
                }
            )
            logger.warning("Revoking drift on %s from %s failed: %s", target, role, exc)

        for (role, schema), grants in places.items():
            touched.add(role)
            # A schema-level revoke takes the dataset grants in the schema with it; the grant
            # pass that follows applies them again.
            if any(kind != "select" for _, kind, *_ in grants):
                try:
                    self.adapter.schema_grant(role, schema, False)
                except Exception as exc:
                    failure(role, schema, exc)
                continue
            for _, _, _, relation, _ in grants:
                # A quoted name the warehouse prints is a case-sensitive object that a
                # revoke by its unquoted name would miss or confuse with another.
                if '"' in relation:
                    continue
                try:
                    self.adapter.dataset_grant(role, f"{schema}.{relation}", False)
                except Exception as exc:
                    failure(role, f"{schema}.{relation}", exc)
        # What a revoke removed is read back: a kind the procedures do not cover stays.
        after, reread = self._read({role: roles[role] for role in touched}, failures)
        held = {grant for grant in held if grant[0] not in reread} | after
        for (role, schema), grants in places.items():
            kept = [g for g in grants if g in held]
            for grant in grants:
                entry = _entry(roles, grant)
                if grant in kept:
                    report["drift"].append(entry)
                else:
                    report["revoked"].append(entry)
                    logger.info(
                        "Revoked %s from %s: the state does not hold it", _named(entry), role
                    )
            noted = (role, schema) in failed or any(
                (role, f"{schema}.{g[3]}") in failed or '"' in (g[3] or "") for g in kept
            )
            if kept and not noted:
                names = ", ".join(_named(_entry(roles, g)) for g in kept)
                failure(role, schema, f"still held after the revoke: {names}")
        return held, None

    @staticmethod
    def _read_failure(exc, group):
        failure = {
            "group": group,
            "kind": "drift",
            "target": "*",
            "operation": "read",
            "error": describe_error(exc),
        }
        logger.warning("Reading the read grants of %s failed: %s", group, failure["error"])
        return failure

    def health(self):
        """The last reconciliation's failures and the grants it added, revoked or found
        drifting, which `vecten issues` reports, and with bound apps each bound group an app
        does not admit."""
        health = self.state.get("access_health", "reconciliation") or {
            "checked_at": None,
            "failures": [],
            "added": [],
            "revoked": [],
            "drift": [],
            "database_future": [],
            "missing_bindings": [],
            "unbound_roles": [],
        }
        if self.apps is not None and self.apps.apps:
            health = {**health, "apps": self.apps.health() + self.app_role_health()}
        return health

    def model_changed(self, datasets):
        with self.lock:
            self.datasets = dict(datasets)
            return self.reconcile()

    def set_denied(self, group, columns):
        self.require_group(group)
        if not isinstance(columns, list) or not all(
            isinstance(c, str) and len(c.split(".")) == 2 and all(c.split(".")) for c in columns
        ):
            raise ValueError("denied columns must be dataset.field strings")
        self.state.put("denied", group, {"columns": sorted(set(columns))})
        return {"group": group, "columns": sorted(set(columns))}

    def denied(self, principal_id):
        denied = set()
        for group in self.state.groups_for(principal_id):
            denied.update((self.state.get("denied", group) or {}).get("columns", []))
        return denied


def _folded(held):
    """Warehouse names compared as the state spells them: dataset sources are matched
    without regard to case."""
    return {(r, k, s.lower(), n.lower() if n else n) for r, k, s, n, _ in held if s}


def _entry(roles, grant, *, place=True):
    role, kind, schema, relation, kind_of = grant
    entry = {"group": roles[role], "role": role, "privilege": PRIVILEGES[kind], "object": kind_of}
    return {**entry, "schema": schema, "relation": relation} if place else entry


def _named(entry):
    place = entry["schema"] + ("." + entry["relation"] if entry["relation"] else "")
    return f"{entry['privilege']} on {place}"
