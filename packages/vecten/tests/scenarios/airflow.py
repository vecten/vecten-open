"""Airflow 3's REST API over recorded task instances, served through an `httpx.MockTransport`.

The scenarios record what each scheduled run did; `vecten issues` reads it back through the
same endpoints it reads on a real stack. Logs are JSON lines as Airflow 3 writes them, served
by the API and, for a deployed environment, written where remote logging puts them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

import httpx

from vecten.issues.context import TaskAttempt, log_key, parse_time

TOKEN = "fake-airflow-jwt"


def failure_log(exc_type: str, exc_value: str, lines: tuple[str, ...] = ()) -> str:
    """A failed task's log: its own lines, then the line carrying the raised exception."""
    stamp = "2026-10-20T02:00:00.000000Z"
    events = [{"timestamp": stamp, "level": "info", "event": line} for line in lines]
    events.append(
        {
            "timestamp": stamp,
            "level": "error",
            "event": "Task failed with exception",
            "error_detail": [
                {
                    "exc_type": exc_type,
                    "exc_value": exc_value,
                    "frames": [{"filename": "airflow_dag.py", "lineno": 45, "name": "sync_one"}],
                }
            ],
        }
    )
    return "\n".join(json.dumps(e) for e in events) + "\n"


@dataclass
class FakeAirflow:
    base_url: str
    log_dir: Path | None = None
    """Where remote logging writes; None keeps logs in the API only, as on the laptop."""
    api_logs: bool = True
    instances: list[dict] = field(default_factory=list)
    logs: dict[tuple, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def record(
        self,
        dag_id: str,
        task_id: str,
        run_id: str,
        state: str,
        *,
        run_after: datetime,
        map_index: int = -1,
        rendered: str | None = None,
        try_number: int = 1,
        log: str | None = None,
    ) -> None:
        doc = {
            "dag_id": dag_id,
            "task_id": task_id,
            "dag_run_id": run_id,
            "map_index": map_index,
            "rendered_map_index": rendered,
            "try_number": try_number,
            "state": state,
            "run_after": run_after.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        self.instances.append(doc)
        if log is None:
            return
        self.logs[(dag_id, run_id, task_id, map_index, try_number)] = log
        if self.log_dir is not None:
            path = self.log_dir / log_key(TaskAttempt.from_api(doc))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(log)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not str(request.url).startswith(self.base_url):
            return httpx.Response(502, json={"detail": "unknown host"})
        path = request.url.path
        if path == "/auth/token" and request.method == "POST":
            return httpx.Response(201, json={"access_token": TOKEN})
        authorized = f"Bearer {TOKEN}" == request.headers.get("Authorization") or (
            f"_token={TOKEN}" in request.headers.get("Cookie", "")
        )
        if not authorized:
            return httpx.Response(401, json={"detail": "not authenticated"})
        if path == "/api/v2/dags/~/dagRuns/~/taskInstances":
            return self._list(request)
        parts = path.split("/")
        if len(parts) == 11 and parts[9] == "logs":
            return self._log(request, parts)
        return httpx.Response(404, json={"detail": "not found"})

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        since = parse_time(params["run_after_gte"])
        states = set(params.get_list("state"))
        rows = [
            i
            for i in self.instances
            if parse_time(i["run_after"]) >= since and (not states or i["state"] in states)
        ]
        rows.sort(key=lambda i: i["run_after"], reverse=True)
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 100))
        page = rows[offset : offset + limit]
        return httpx.Response(200, json={"task_instances": page, "total_entries": len(rows)})

    def _log(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        dag_id, run_id, task_id = unquote(parts[4]), unquote(parts[6]), unquote(parts[8])
        key = (
            dag_id,
            run_id,
            task_id,
            int(request.url.params.get("map_index", -1)),
            int(parts[10]),
        )
        text = self.logs.get(key) if self.api_logs else None
        if text is None:
            return httpx.Response(404, json={"detail": "no log"})
        content = [json.loads(line) for line in text.splitlines() if line.strip()]
        return httpx.Response(200, json={"content": content, "continuation_token": None})
