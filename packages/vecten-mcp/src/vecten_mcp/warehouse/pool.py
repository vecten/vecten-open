"""Connections never cross principals; memberships live until the last session closes."""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock

from vecten_mcp.failures import NoWarehouseAccess


@dataclass
class Pool:
    principal: dict
    sessions: dict[str, tuple[float, float]] = field(default_factory=dict)
    connections: list = field(default_factory=list)
    busy: set[int] = field(default_factory=set)


class PrincipalPools:
    def __init__(
        self, adapter, *, idle_timeout=900, max_connections=2, clock=time.time, validate=None
    ):
        self.adapter, self.clock = adapter, clock
        self.idle_timeout, self.max_connections = idle_timeout, max_connections
        self.validate = validate
        self.pools = {}
        self.lock = RLock()

    @contextmanager
    def acquire(self, principal, session_id, expires_at, roles=frozenset({"analyst"})):
        if not principal["enabled"] or not principal["provisioned"]:
            raise NoWarehouseAccess()
        with self.lock:
            if self.validate:
                principal, roles = self.validate(principal, session_id, roles)
            now = self.clock()
            if expires_at <= now:
                raise ValueError("session expired")
            self.expire()
            pid = principal["id"]
            pool = self.pools.get(pid)
            if pool is None:
                self.adapter.open_membership(principal["warehouse_name"])
                pool = self.pools[pid] = Pool(principal)
            connection = next((c for c in pool.connections if id(c) not in pool.busy), None)
            if connection is None:
                if len(pool.connections) >= self.max_connections:
                    raise ValueError("principal connection pool is busy; retry")
                try:
                    connection = self.adapter.connect_principal(principal, set(roles), expires_at)
                except BaseException:
                    if not pool.sessions:
                        self._close(pid)
                    raise
                pool.connections.append(connection)
            pool.sessions[session_id] = (expires_at, now)
            pool.busy.add(id(connection))
        try:
            yield connection
        except BaseException:
            with self.lock:
                if connection in pool.connections:
                    pool.connections.remove(connection)
                    connection.close()
            raise
        finally:
            with self.lock:
                pool.busy.discard(id(connection))
                if session_id in pool.sessions:
                    pool.sessions[session_id] = (expires_at, self.clock())

    def _close(self, pid):
        pool = self.pools[pid]
        try:
            for connection in pool.connections:
                connection.close()
        finally:
            # Keep the pool until revocation succeeds so maintenance retries failures.
            self.adapter.close_membership(pool.principal["warehouse_name"])
            del self.pools[pid]

    def revoke_session(self, session_id):
        with self.lock:
            for pid, pool in list(self.pools.items()):
                pool.sessions.pop(session_id, None)
                if not pool.sessions:
                    self._close(pid)

    def disable_principal(self, principal_id):
        with self.lock:
            if principal_id in self.pools:
                self.pools[principal_id].sessions.clear()
                self._close(principal_id)

    def expire(self):
        with self.lock:
            now = self.clock()
            for pid, pool in list(self.pools.items()):
                for sid, (expiry, used) in list(pool.sessions.items()):
                    if expiry <= now or (not pool.busy and now - used >= self.idle_timeout):
                        del pool.sessions[sid]
                if not pool.sessions:
                    self._close(pid)

    def close(self):
        with self.lock:
            for pid in list(self.pools):
                self._close(pid)
