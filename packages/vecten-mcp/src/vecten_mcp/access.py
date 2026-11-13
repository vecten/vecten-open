"""Desired grants are persisted only after their warehouse operation succeeds."""

import hashlib
import json
import logging
import time
from collections import defaultdict
from threading import RLock

from sqlalchemy import delete, insert, select

from vecten_mcp.apps import APP_PRINCIPALS
from vecten_mcp.failures import describe_error
from vecten_mcp.identity import warehouse_name
from vecten_mcp.state import groups, memberships
from vecten_shared.identity_setup import ACCESS_VERSION

logger = logging.getLogger(__name__)
# Records kind: app principal id -> {"roles": every warehouse role it may hold, "failed":
# the last sync failure until a sync succeeds}.
APP_ROLES = "app_roles"

CONVENTION_SCHEMAS = frozenset({"core", "marts", "staging", "snapshots"})
PRIVILEGES = {"usage": "USAGE", "select": "SELECT", "future": "FUTURE SELECT"}
OUTDATED_FIX = (
    "An ACCOUNTADMIN reruns `vecten mcp provisioner-script --env <env>` and confirms it with "
    "--confirm-run: until then read grants the state does not hold are neither listed nor "
    "revoked."
)


def in_scope(schema):
    """The schemas whose read grants the state database owns for every managed role: the
    convention's, and every `raw_` schema, a prefix the convention keeps for loaded sources
    and the bound READ_GRANT and the Postgres helpers already accept."""
    return schema in CONVENTION_SCHEMAS or schema.startswith("raw_")


class AccessService:
    def __init__(self, state, adapter, *, invalidate=None):
        self.state, self.adapter = state, adapter
        self.invalidate = invalidate or (lambda pid: None)
        self.datasets = {}
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
            return [dict(row) for row in conn.execute(select(groups)).mappings()]

    def create_group(self, name):
        role = warehouse_name(name, set(), override=name)
        with self.lock, self.state.transaction() as conn:
            if self.state.group(name):
                raise ValueError("group already exists")
            conn.execute(insert(groups).values(name=name, warehouse_role=role))
            self.adapter.create_group(role)
            self.state.put("provisioner_registry", role, {"kind": "group"}, conn=conn)
        self.admit()
        return {"name": name, "warehouse_role": role}

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
                conn.execute(delete(groups).where(groups.c.name == name))
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
        if kind == "schema":
            if target not in {"core", "marts", "staging", "snapshots"} and not target.startswith(
                "raw_"
            ):
                raise ValueError("schema grants are limited to convention schemas")
            if not target.replace("_", "").isalnum():
                raise ValueError("invalid schema name")
            return target
        if kind != "dataset" or target not in self.datasets:
            raise ValueError("unknown dataset")
        return self.datasets[target]

    def _apply(self, grant, source, allowed):
        role = self.require_group(grant["group"])["warehouse_role"]
        method = (
            self.adapter.schema_grant if grant["kind"] == "schema" else self.adapter.dataset_grant
        )
        method(role, source, allowed)

    def grant(self, group, kind, target):
        self.require_group(group)
        source = self._source(kind, target)
        row = {"group": group, "kind": kind, "target": target}
        with self.lock:
            self._apply(row, source, True)
            key = self._key(group, kind, target)
            self.state.put("grants", key, row)
            self.state.put("applied_grants", key, dict(row, source=source))
            self.admit()
        return row

    def revoke(self, group, kind, target):
        self.require_group(group)
        key = self._key(group, kind, target)
        with self.lock:
            row = self.state.get("grants", key)
            applied = self.state.get("applied_grants", key)
            if applied:
                self._apply(applied, applied["source"], False)
            self.state.remove("grants", key)
            self.state.remove("applied_grants", key)
            reconciliation = self.reconcile()
        result = row or {"group": group, "kind": kind, "target": target}
        if not reconciliation["complete"]:
            result = dict(result, reconciliation=reconciliation)
        return result

    def list_grants(self):
        return [row for _, row in self.state.scan("grants")]

    def reconcile(self):
        failures, blocked, reconciled = [], set(), 0
        report = {"added": [], "revoked": [], "drift": [], "database_future": []}

        def failed(key, row, operation, exc):
            blocked.add(key)
            failure = dict(row, operation=operation, error=describe_error(exc))
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
            for key, old in applied.items():
                row = desired.get(key)
                missing = (
                    row is not None
                    and row["kind"] == "dataset"
                    and row["target"] not in self.datasets
                )
                if (
                    row is None
                    or missing
                    or self._source(row["kind"], row["target"]) != old["source"]
                ):
                    try:
                        self._apply(old, old["source"], False)
                        self.state.remove("applied_grants", key)
                    except Exception as exc:
                        failed(key, old, "revoke", exc)
            held, outdated = self._converge(desired, report, failures)
            for key, row in desired.items():
                if key in blocked or (
                    row["kind"] == "dataset" and row["target"] not in self.datasets
                ):
                    continue
                try:
                    source = self._source(row["kind"], row["target"])
                    role = self.require_group(row["group"])["warehouse_role"]
                    pieces = self._pieces(role, row["kind"], source)
                    absent = key not in applied if held is None else not pieces <= _folded(held)
                    self._apply(row, source, True)
                    self.state.put("applied_grants", key, dict(row, source=source))
                    reconciled += 1
                    if absent:
                        report["added"].append(dict(row))
                        logger.info("Granted %s %s to %s", row["kind"], row["target"], row["group"])
                except Exception as exc:
                    failed(key, row, "grant", exc)
        health = {"checked_at": time.time(), "failures": failures, **report}
        if outdated:
            health["outdated"] = outdated
        self.state.put("access_health", "reconciliation", health)
        self.admit()
        return {"reconciled": reconciled, "complete": not failures, "failures": failures}

    def _pieces(self, role, kind, source):
        """The read grants, as `read_grants` names them without the object kind, that one
        desired grant consists of."""
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
            installed = self.adapter.access_version()
        except Exception as exc:
            failures.append(self._read_failure(exc, "*"))
            return None, None
        if installed < ACCESS_VERSION:
            outdated = {"installed": installed, "required": ACCESS_VERSION, "fix": OUTDATED_FIX}
            logger.warning("Access procedures are version %s; %s", installed, OUTDATED_FIX)
            return None, outdated
        roles = {row["warehouse_role"]: row["name"] for row in self.list_groups()}
        held, readable = self._read(roles, failures)
        if not readable:
            return None, None
        whole, datasets = set(), defaultdict(set)
        for row in desired.values():
            group = self.state.group(row["group"])
            if group is None or (row["kind"] == "dataset" and row["target"] not in self.datasets):
                continue
            source = self._source(row["kind"], row["target"])
            if row["kind"] == "schema":
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
            if not in_scope(schema) or (role, schema) in whole:
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
