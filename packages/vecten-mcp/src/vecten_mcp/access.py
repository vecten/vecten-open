"""Desired grants are persisted only after their warehouse operation succeeds."""

import hashlib
import json
import logging
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
DRIFT_MODES = ("revoke", "report")
OUTDATED_FIX = (
    "An ACCOUNTADMIN reruns `vecten mcp provisioner-script --env <env>` and confirms it with "
    "--confirm-run: until then read grants the state does not hold are neither listed nor "
    "revoked."
)


def in_scope(schema):
    """The schemas whose read grants the state database owns for every managed role."""
    return schema in CONVENTION_SCHEMAS or schema.startswith("raw_")


class AccessService:
    def __init__(self, state, adapter, *, invalidate=None):
        self.state, self.adapter = state, adapter
        self.invalidate = invalidate or (lambda pid: None)
        self.datasets = {}
        self.lock = RLock()

    def seed_defaults(self):
        # No setup grants these: the first reconciliation applies them. Recording them once
        # keeps an administrator's later revocation from coming back on restart.
        with self.lock, self.state.transaction() as conn:
            if self.state.get("bootstrap", "default_grants", conn=conn):
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
        report = {"added": [], "revoked": [], "drift": []}

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
        return {"reconciled": reconciled, "complete": not failures, "failures": failures}

    def _pieces(self, role, kind, source):
        """The read grants, as `read_grants` names them, that one desired grant consists of."""
        if kind == "schema":
            return {(role, "usage", source, None), (role, "future", source, None)}
        schema, relation = source.lower().split(".")[-2:]
        return {(role, "usage", schema, None), (role, "select", schema, relation)}

    def _converge(self, desired, report, failures):
        """Revoke, or list under `report`, the read grants managed roles hold that the state
        does not; returns what they held after it, or None when it could not be read."""
        try:
            installed = self.adapter.access_version()
        except Exception as exc:
            failures.append(self._read_failure(exc))
            return None, None
        if installed < ACCESS_VERSION:
            outdated = {"installed": installed, "required": ACCESS_VERSION, "fix": OUTDATED_FIX}
            logger.warning("Access procedures are version %s; %s", installed, OUTDATED_FIX)
            return None, outdated
        roles = {row["warehouse_role"]: row["name"] for row in self.list_groups()}
        try:
            held = set(self.adapter.read_grants(set(roles)))
        except Exception as exc:
            failures.append(self._read_failure(exc))
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
        for role, kind, schema, relation in sorted(held, key=lambda g: tuple(map(str, g))):
            if role not in roles or not in_scope(schema) or (role, schema) in whole:
                continue
            allowed = datasets.get((role, schema), set())
            if (kind == "usage" and allowed) or (kind == "select" and relation.lower() in allowed):
                continue
            places[(role, schema)].append((kind, relation))
        mode = (self.state.get("settings", "reconcile_drift") or {}).get("value", "revoke")
        for (role, schema), grants in places.items():
            entries = [
                {
                    "group": roles[role],
                    "role": role,
                    "privilege": PRIVILEGES[kind],
                    "schema": schema,
                    "relation": relation,
                }
                for kind, relation in grants
            ]
            if mode == "report":
                report["drift"] += entries
                for entry in entries:
                    logger.info("Drift: %s holds %s the state does not", role, _named(entry))
                continue
            try:
                # A schema-level revoke takes the dataset grants in the schema with it; the
                # grant pass that follows applies them again.
                if any(kind != "select" for kind, _ in grants):
                    self.adapter.schema_grant(role, schema, False)
                    held = {g for g in held if (g[0], g[2]) != (role, schema)}
                else:
                    for _, relation in grants:
                        self.adapter.dataset_grant(role, f"{schema}.{relation}", False)
                        held.discard((role, "select", schema, relation))
            except Exception as exc:
                failure = {
                    "group": roles[role],
                    "kind": "drift",
                    "target": schema,
                    "operation": "revoke",
                    "error": describe_error(exc),
                }
                failures.append(failure)
                logger.warning(
                    "Revoking drift on %s from %s failed: %s", schema, role, failure["error"]
                )
                continue
            report["revoked"] += entries
            for entry in entries:
                logger.info("Revoked %s from %s: the state does not hold it", _named(entry), role)
        return held, None

    @staticmethod
    def _read_failure(exc):
        failure = {
            "group": "*",
            "kind": "drift",
            "target": "*",
            "operation": "read",
            "error": describe_error(exc),
        }
        logger.warning("Reading the warehouse's read grants failed: %s", failure["error"])
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
    return {(r, k, s.lower(), n.lower() if n else n) for r, k, s, n in held}


def _named(entry):
    place = entry["schema"] + ("." + entry["relation"] if entry["relation"] else "")
    return f"{entry['privilege']} on {place}"
