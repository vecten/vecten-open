"""Reconcile shipped review evidence against the model that actually serves queries."""

import copy
import json
import re
import time
from dataclasses import asdict

from sqlalchemy import select, text

from vecten_mcp.feedback import digest, retention_seconds
from vecten_mcp.state import records

COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
PULL_REQUEST = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9][0-9]*")


UNREVIEWED = "merged without an approving review; the gap stays open"
VERIFICATION_KEYS = ("pull_request", "merge_commit", "author", "reviewers", "merged_at")


def _commits(manifest):
    if not isinstance(manifest, dict) or not COMMIT.fullmatch(str(manifest.get("commit", ""))):
        return None
    ancestors = manifest.get("ancestors", [])
    if not isinstance(ancestors, list) or not all(
        isinstance(value, str) and COMMIT.fullmatch(value) for value in ancestors
    ):
        return None
    return {manifest["commit"], *ancestors}


def _entries(manifest, key, *, reviewed):
    commits = _commits(manifest)
    entries = manifest.get(key, []) if commits else []
    if not isinstance(entries, list):
        return []
    accepted = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        reviewers, author = entry.get("reviewers"), entry.get("author")
        if (
            not isinstance(entry.get("gap"), str)
            or not 1 <= len(entry["gap"]) <= 100
            or not isinstance(entry.get("merge_commit"), str)
            or entry["merge_commit"] not in commits
            or not PULL_REQUEST.fullmatch(str(entry.get("pull_request", "")))
            or not isinstance(author, str)
            or not author
            or len(author) > 100
            or (
                reviewed
                and (
                    not isinstance(reviewers, list)
                    or not reviewers
                    or not all(
                        isinstance(r, str) and 0 < len(r) <= 100 and r != author for r in reviewers
                    )
                )
            )
            or not isinstance(entry.get("merged_at"), str)
            or not isinstance(entry.get("metrics"), list)
            or not entry["metrics"]
            or len(entry["metrics"]) > 100
            or not all(isinstance(m, str) and len(m) <= 256 for m in entry["metrics"])
        ):
            continue
        accepted.append(entry)
    return accepted


def verified_entries(manifest):
    return _entries(manifest, "resolutions", reviewed=True)


def unreviewed_entries(manifest):
    return _entries(manifest, "unreviewed", reviewed=False)


class FeedbackResolution:
    def __init__(self, runtime, manifest_path, *, clock=time.time):
        self.runtime, self.state = runtime, runtime.state
        self.manifest_path, self.clock = manifest_path, clock

    def reconcile(self):
        if not self.manifest_path or not self.manifest_path.is_file() or not self.runtime.model:
            return
        manifest = json.loads(self.manifest_path.read_text())
        entries = verified_entries(manifest)
        unreviewed = unreviewed_entries(manifest)
        if not entries and not unreviewed:
            return
        model = self.runtime.model
        with model._lock:
            live = model.current and model.current.commit == manifest.get("commit")
            snapshot = None
            with self.state.transaction() as conn:
                if self.state.engine.dialect.name == "sqlite":
                    conn.execute(text("BEGIN IMMEDIATE"))
                entries = entries + self._unreviewed(unreviewed, conn)
                verified_metrics = set()
                for entry in sorted(entries, key=lambda entry: entry["merged_at"], reverse=True):
                    if live and any(
                        metric not in model.current.metrics for metric in entry["metrics"]
                    ):
                        continue
                    verification = {
                        key: entry[key]
                        for key in (*VERIFICATION_KEYS, "confirmed_by")
                        if key in entry
                    }
                    metrics = set(entry["metrics"]) - verified_metrics
                    verified_metrics.update(metrics)
                    if live and any(
                        model.current.metrics[m].get("verified") != verification for m in metrics
                    ):
                        if snapshot is None:
                            snapshot = copy.deepcopy(model.current)
                        for metric in metrics:
                            snapshot.metrics[metric]["verified"] = verification
                    gap = self.state.get("gaps", entry["gap"], conn=conn, lock=True)
                    if gap is None:
                        continue
                    before = copy.deepcopy(gap)
                    gap["pull_request"] = entry["pull_request"]
                    gap["verification"] = verification
                    if live:
                        if gap.get("resolved_merge_commit") != entry["merge_commit"]:
                            gap.update(
                                status="resolved",
                                delivery_status="live",
                                resolved_commit=manifest["commit"],
                                resolved_merge_commit=entry["merge_commit"],
                            )
                        queued = (
                            set(gap.get("notice_reporters", []))
                            if gap.get("notice_merge_commit") == entry["merge_commit"]
                            else set()
                        )
                        for person in set(gap.get("reporters", [])) - queued:
                            key = digest([person, entry["gap"], entry["merge_commit"]])
                            if self.state.get("gap_notices", key, conn=conn) is None:
                                self.state.put(
                                    "gap_notices",
                                    key,
                                    {
                                        "principal_id": person,
                                        "at": self.clock(),
                                        "gap": entry["gap"],
                                        "definitions": entry["metrics"],
                                        "commit": manifest["commit"],
                                        "pull_request": entry["pull_request"],
                                    },
                                    expires_at=self.clock() + retention_seconds(self.state, conn),
                                    conn=conn,
                                )
                            queued.add(person)
                        gap["notice_reporters"] = sorted(queued)
                        gap["notice_merge_commit"] = entry["merge_commit"]
                    elif gap.get("status") != "resolved":
                        gap["delivery_status"] = "merged, waiting for build"
                    if gap != before:
                        self.state.put("gaps", entry["gap"], gap, conn=conn)
                if snapshot and entries:
                    saved = self.state.get("semantic_model", "live", conn=conn)
                    saved["model"] = asdict(snapshot)
                    self.state.put("semantic_model", "live", saved, conn=conn)
            if snapshot and entries:
                model.current = snapshot

    def _unreviewed(self, entries, conn):
        """Record each unreviewed fix on its gap; return the ones an admin confirmed.

        A confirmation names the merge commit the admin saw, so a later unreviewed merge of
        the same gap is not covered by it.
        """
        confirmed = []
        for entry in entries:
            gap = self.state.get("gaps", entry["gap"], conn=conn, lock=True)
            if gap is None:
                continue
            confirmation = gap.get("confirmation") or {}
            if confirmation.get("merge_commit") == entry["merge_commit"]:
                confirmed.append(
                    {**entry, "reviewers": [], "confirmed_by": confirmation.get("email")}
                )
                continue
            if gap.get("status") == "resolved":
                continue
            before = copy.deepcopy(gap)
            gap.update(
                unreviewed_fix={
                    key: entry[key]
                    for key in ("pull_request", "merge_commit", "author", "merged_at")
                },
                pull_request=entry["pull_request"],
                delivery_status=UNREVIEWED,
            )
            if gap != before:
                self.state.put("gaps", entry["gap"], gap, conn=conn)
        return confirmed

    def notices(self, caller):
        if caller.principal["kind"] != "human":
            return []
        session = self.state.get("sessions", caller.session_id)
        if not session or session["principal_id"] != caller.principal["id"]:
            return []
        # Normal calls use a read-only, principal-filtered query. Acquire write locks only
        # when there is something to deliver, and recheck after locking for concurrency.
        with self.state.engine.connect() as conn:
            keys = (
                conn.execute(
                    select(records.c.key)
                    .where(
                        records.c.kind == "gap_notices",
                        records.c.body["principal_id"].as_string() == caller.principal["id"],
                        records.c.body["delivered_session"].as_string().is_(None),
                        records.c.body["at"].as_float() < session["started_at"],
                        (records.c.expires_at.is_(None) | (records.c.expires_at > self.clock())),
                    )
                    .limit(100)
                )
                .scalars()
                .all()
            )
        if not keys:
            return []
        with self.state.transaction() as conn:
            if self.state.engine.dialect.name == "sqlite":
                conn.execute(text("BEGIN IMMEDIATE"))
            result = []
            for key in keys:
                notice = self.state.get("gap_notices", key, now=self.clock(), conn=conn, lock=True)
                if not notice or notice.get("delivered_session"):
                    continue
                result.append(
                    {k: notice[k] for k in ("gap", "definitions", "commit", "pull_request")}
                )
                notice["delivered_session"] = caller.session_id
                self.state.put(
                    "gap_notices",
                    key,
                    notice,
                    expires_at=notice["at"] + retention_seconds(self.state, conn),
                    conn=conn,
                )
            return result
