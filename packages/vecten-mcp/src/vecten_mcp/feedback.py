"""Replay-safe folding of persisted signals into administrator-visible gaps."""

import hashlib
import json
import time

from sqlalchemy import and_, delete, exists, func, or_, select, text, update

from vecten_mcp.feedback_signals import audit_signal, report_signal
from vecten_mcp.state import audit_log, records
from vecten_mcp.tool_service import evidence


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def retention_seconds(state, conn=None):
    setting = state.get("settings", "audit_retention_days", conn=conn) or {}
    days = setting.get("value", 90)
    return (days if type(days) is int and 1 <= days <= 3650 else 90) * 86400


class FeedbackService:
    def __init__(self, runtime, *, clock=time.time):
        self.runtime, self.state, self.clock = runtime, runtime.state, clock
        self.last_cleanup = None

    def _cleanup(self, conn):
        now = self.clock()
        if self.last_cleanup is not None and now - self.last_cleanup < 86400:
            return
        sources = records.alias("sources")

        def audit_exists(prefix):
            return exists().where(audit_log.c.id == func.substr(records.c.key, len(prefix) + 1))

        report_exists = exists().where(
            sources.c.kind == "feedback", sources.c.key == func.substr(records.c.key, 8)
        )
        # Keep markers as long as their sources exist: retention policy changes cannot
        # make a still-retained audit/report count again after its marker expires.
        conn.execute(
            delete(records).where(
                records.c.kind == "feedback_processed",
                or_(
                    and_(records.c.key.startswith("audit:"), ~audit_exists("audit:")),
                    and_(records.c.key.startswith("fallback:"), ~audit_exists("fallback:")),
                    and_(records.c.key.startswith("report:"), ~report_exists),
                ),
            )
        )
        conn.execute(
            update(records)
            .where(
                records.c.kind.in_(["feedback", "feedback_pending", "gap_notices"]),
                records.c.expires_at.is_(None),
            )
            .values(
                expires_at=func.coalesce(records.c.body["at"].as_float(), now)
                + retention_seconds(self.state, conn)
            )
        )

    def _mark(self, key, gap, conn):
        self.state.put("feedback_processed", key, {"gap": gap}, conn=conn)

    def _fold(self, signal, key, conn):
        previous = self.state.get("feedback_processed", key, conn=conn)
        if previous is not None:
            return previous["gap"], False
        if signal is None:
            self._mark(key, None, conn)
            return None, False
        if signal["fallback"]:
            return self._fallback(signal, key, conn)
        subject = signal["datasets"] or signal["names"]
        # Without a dataset or a missing name there is no evidence of equivalence.
        namespace = "datasets" if signal["datasets"] else "names"
        identifier = (
            "gap-"
            + digest([signal["category"], signal["reason"], namespace, subject or [key]])[:24]
        )
        gap = self.state.get("gaps", identifier, conn=conn, lock=True) or {
            "category": signal["category"],
            "reason": signal["reason"],
            "datasets": signal["datasets"],
            "columns": [],
            "names": signal["names"],
            "occurrence_count": 0,
            "first_seen": signal["at"],
            "last_seen": signal["at"],
            "status": "open",
            "reporters": [],
        }
        gap["occurrence_count"] += 1
        gap["first_seen"] = min(gap["first_seen"], signal["at"])
        gap["last_seen"] = max(gap["last_seen"], signal["at"])
        gap["columns"] = sorted(set(gap["columns"]) | set(signal["columns"]))[:100]
        gap["names"] = sorted(set(gap["names"]) | set(signal["names"]))[:100]
        if key.startswith("report:") and signal["principal_id"]:
            gap["reporters"] = sorted(set(gap["reporters"]) | {signal["principal_id"]})
        redact = bool((self.state.get("settings", "redact", conn=conn) or {}).get("value"))
        incoming = evidence(signal, redact=redact)
        if not gap.get("evidence") or signal["at"] >= gap["last_seen"]:
            gap["evidence"] = incoming
        self.state.put("gaps", identifier, gap, conn=conn)
        self._mark(key, identifier, conn)
        if signal.get("chained_sql"):
            self._fallback(signal, "fallback:" + signal["id"], conn)
        return identifier, True

    def _fallback(self, signal, key, conn):
        stored = conn.execute(select(audit_log).where(audit_log.c.id == signal["chain"])).first()
        if stored is None:
            # The asynchronous audit queue can flush concurrent calls out of order.
            self.state.put(
                "feedback_pending",
                key,
                signal,
                expires_at=self.clock() + retention_seconds(self.state, conn),
                conn=conn,
            )
            self._mark(key, None, conn)
            return None, False
        refusal = {
            **(stored.body if isinstance(stored.body, dict) else {}),
            "id": stored.id,
            "at": stored.at,
        }
        self.state.remove("feedback_pending", key, conn=conn)
        if (
            refusal.get("resolution") != "refused"
            or refusal.get("principal_id") != signal["principal_id"]
            or refusal.get("session_id") != signal["session_id"]
        ):
            self._mark(key, None, conn)
            return None, False
        identifier, _ = self._fold(audit_signal(refusal), "audit:" + refusal["id"], conn)
        if identifier:
            gap = self.state.get("gaps", identifier, conn=conn, lock=True)
            redact = bool((self.state.get("settings", "redact", conn=conn) or {}).get("value"))
            gap["evidence"]["agent_sql"] = evidence(signal, redact=redact)["agent_sql"]
            gap["columns"] = sorted(set(gap["columns"]) | set(signal["columns"]))[:100]
            gap["last_seen"] = max(gap["last_seen"], signal["at"])
            self.state.put("gaps", identifier, gap, conn=conn)
        self._mark(key, identifier, conn)
        return identifier, False

    def fold(self):
        with self.state.transaction() as conn:
            if self.state.engine.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(7361616)"))
            else:
                conn.execute(text("BEGIN IMMEDIATE"))
            self._cleanup(conn)
            processed = records.alias("processed")
            audit_done = exists().where(
                processed.c.kind == "feedback_processed",
                processed.c.key == "audit:" + audit_log.c.id,
            )
            rows = conn.execute(
                select(audit_log)
                .where(
                    ~audit_done,
                    audit_log.c.body["tool"].as_string().in_(["query_metrics", "sql-exec"]),
                    audit_log.c.body["resolution"]
                    .as_string()
                    .in_(["refused", "error", "agent_sql"]),
                )
                .order_by(audit_log.c.at, audit_log.c.id)
                .limit(500)
            ).all()
            count = 0
            for stored in rows:
                row = {
                    **(stored.body if isinstance(stored.body, dict) else {}),
                    "id": stored.id,
                    "at": stored.at,
                }
                _, added = self._fold(audit_signal(row), "audit:" + row["id"], conn)
                count += added
            report_done = exists().where(
                processed.c.kind == "feedback_processed",
                processed.c.key == "report:" + records.c.key,
            )
            reports = (
                conn.execute(
                    select(records.c.body)
                    .where(
                        records.c.kind == "feedback",
                        ~report_done,
                        records.c.expires_at.is_(None) | (records.c.expires_at > self.clock()),
                    )
                    .order_by(records.c.key)
                    .limit(500)
                )
                .scalars()
                .all()
            )
            for row in reports:
                _, added = self._fold(report_signal(row), "report:" + row["id"], conn)
                count += added
            arrived = exists().where(audit_log.c.id == records.c.body["chain"].as_string())
            pending = conn.execute(
                select(records.c.key, records.c.body)
                .where(
                    records.c.kind == "feedback_pending",
                    arrived,
                    records.c.expires_at > self.clock(),
                )
                .limit(500)
            ).all()
            for key, signal in pending:
                self._fallback(signal, key, conn)
        if self.last_cleanup is None or self.clock() - self.last_cleanup >= 86400:
            self.last_cleanup = self.clock()
        return count
