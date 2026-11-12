"""Desired grants are persisted only after their warehouse operation succeeds."""

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from threading import RLock

from sqlalchemy import delete, insert, select

from vecten_mcp.failures import describe_error
from vecten_mcp.identity import warehouse_name
from vecten_mcp.state import groups, memberships
from vecten_shared.identity_setup import ACCESS_VERSION

logger = logging.getLogger(__name__)

CONVENTION_SCHEMAS = frozenset({"core", "marts", "staging", "snapshots"})
PRIVILEGES = {"usage": "USAGE", "select": "SELECT", "future": "FUTURE SELECT"}
OUTDATED_FIX = (
    "An ACCOUNTADMIN reruns `vecten mcp provisioner-script --env <env>` and confirms it with "
    "--confirm-run: until then area grants wait, and read grants the state does not hold "
    "are neither listed nor revoked."
)


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

    def _key(self, group, kind, target):
        return hashlib.sha256(json.dumps([group, kind, target]).encode()).hexdigest()

    def _source(self, kind, target):
        if kind not in KINDS:
            raise ValueError("a grant's kind is dataset, schema or area")
        if kind == "area":
            if not isinstance(target, str) or target not in self.areas:
                raise Undeclared(
                    f"no [areas.{target}] in vecten.toml: declare the area {target} and ship "
                    "before granting it"
                )
            return area_schemas(target)
        if kind == "schema":
            area = AREA_SCHEMA.fullmatch(target) if isinstance(target, str) else None
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
        return self.datasets[target]

    def _current(self, kind, source):
        """Whether the grant needs procedures as recent as this server: an area schema is
        outside what an older READ_GRANT accepts."""
        schemas = source if kind == "area" else [source] if kind == "schema" else []
        return any(AREA_SCHEMA.fullmatch(schema) for schema in schemas)

    def _outdated(self):
        installed = self.adapter.access_version()
        if installed < ACCESS_VERSION:
            return {"installed": installed, "required": ACCESS_VERSION, "fix": OUTDATED_FIX}
        return None

    def _apply(self, grant, source, allowed):
        """The relation kinds the warehouse refused, each with its message; the grant holds
        for every other kind."""
        role = self.require_group(grant["group"])["warehouse_role"]
        if grant["kind"] == "dataset":
            return list(self.adapter.dataset_grant(role, source, allowed) or [])
        refused = []
        for schema in source if grant["kind"] == "area" else [source]:
            kinds = self.adapter.schema_grant(role, schema, allowed) or []
            refused += [f"{schema}: {kind}" for kind in kinds]
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
        with self.lock:
            refused = self._apply(row, source, True)
            key = self._key(group, kind, target)
            self.state.put("grants", key, row)
            self.state.put("applied_grants", key, dict(row, source=source))
        return dict(row, refused=refused) if refused else row

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
                try:
                    role = self.require_group(row["group"])["warehouse_role"]
                    pieces = self._pieces(role, row["kind"], source)
                    absent = key not in applied if held is None else not pieces <= _folded(held)
                    refused = self._apply(row, source, True)
                    self.state.put("applied_grants", key, dict(row, source=source))
                    reconciled += 1
                    if refused:
                        failures.append(dict(row, operation="grant", error="; ".join(refused)))
                    if absent:
                        report["added"].append(dict(row))
                        logger.info("Granted %s %s to %s", row["kind"], row["target"], row["group"])
                except Exception as exc:
                    failed(key, row, "grant", exc)
        health = {"checked_at": time.time(), "failures": failures, **report}
        if outdated:
            health["outdated"] = outdated
        self.state.put("access_health", "reconciliation", health)
        return {"reconciled": reconciled, "complete": not failures, "failures": failures}

    def _source_of(self, row):
        """What a desired grant reaches now, or None when its dataset is not in the model or
        its area is not declared: such a grant stays recorded and is applied nowhere."""
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
        drifting, which `vecten issues` reports."""
        return self.state.get("access_health", "reconciliation") or {
            "checked_at": None,
            "failures": [],
            "added": [],
            "revoked": [],
            "drift": [],
            "database_future": [],
        }

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
