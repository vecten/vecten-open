"""Rotate only impersonator's own password without closing principal query connections."""

import json
import secrets
import time
from threading import RLock

from psycopg import sql

from vecten_shared.credentials import password_candidates


class PasswordRotation:
    def __init__(self, state, provider, connect, *, clock=time.time):
        self.state, self.provider, self.connect, self.clock = state, provider, connect, clock
        self.lock = RLock()

    def tick(self):
        previous = self.state.get("rotation", "impersonator")
        if previous is None:
            self.state.put("rotation", "impersonator", {"at": self.clock()})
        elif self.clock() - previous["at"] >= 30 * 86400:
            self.rotate()

    def rotate(self):
        with self.lock:
            original = self.provider.get_secret("MCP_IMPERSONATOR_PASSWORD")
            connection = None
            for current in password_candidates(original):
                try:
                    connection = self.connect(current)
                    break
                except Exception:
                    continue
            if connection is None:
                raise ValueError("impersonator authentication failed during rotation")
            try:
                pending = secrets.token_urlsafe(48)
                # Persist before changing the database. Either candidate works at every crash
                # boundary; a retry identifies the active one by authenticating it.
                self.provider.put_secret(
                    "MCP_IMPERSONATOR_PASSWORD",
                    json.dumps(
                        {
                            "current": current,
                            "pending": pending,
                        }
                    ),
                )
                connection.execute(
                    sql.SQL("ALTER ROLE impersonator PASSWORD {}").format(sql.Literal(pending))
                )
                # A timeout can follow a committed ALTER; replace the envelope only on success.
                self.provider.put_secret("MCP_IMPERSONATOR_PASSWORD", pending)
                self.state.put("rotation", "impersonator", {"at": self.clock()})
            finally:
                connection.close()
