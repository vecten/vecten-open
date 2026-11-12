"""Airflow task failures: the REST API's task instances, grouped by DAG, task key and class.

An issue is open while the newest finished attempt of its task key failed; its occurrences
are the failures since the last green run, so a green run at or after the fix clears it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from vecten.issues.context import IssueContext, SourceUnavailable, TaskAttempt, iso
from vecten.issues.dbt import FAILED as DBT_FAILED
from vecten.issues.dbt import node_issue_id
from vecten.issues.logs import parse_log
from vecten.issues.resolutions import DBT_DAG, airflow_resolution, sync_connector
from vecten_platform.dbt.artifacts import run_keys
from vecten_shared.issues import (
    Issue,
    airflow_issue_id,
    cap,
    failure_class,
    is_sync_task,
    redact,
    sync_task_key,
    untrusted,
)

# Tests replace this with an `httpx.MockTransport`; production leaves it None.
TRANSPORT: httpx.BaseTransport | None = None
PAGE = 100
# Past this many task instances in the window the scan stops and reports the source as
# incomplete, since an unread page can hold the newest failure of a task.
MAX_ROWS = 50_000
EXCERPT_LIMIT = 4000
ERROR_LIMIT = 500
FINISHED = ("success", "failed", "skipped")
# Only a run that succeeded clears a failure; a skipped task never ran its code.
GREEN = {"success"}
FAILED = "failed"
# The sync DAG's `check` task fails whenever an object did, and the dbt DAG's `build` and
# `convention` tasks fail whenever a node did; the object or node is the issue, not them.
SUMMARY_TASKS = {"check"}
DBT_RESULT_TASKS = {"build", "convention"}
NO_LOG_CLASS = "TaskFailed"


class AirflowClient:
    """Airflow's REST API, signed in with Airflow's own login."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        verify: Any = True,
    ):
        self.base_url = base_url.rstrip("/")
        self._login = {"username": username, "password": password}
        self._verify = verify
        self._http: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        http = httpx.Client(timeout=30, verify=self._verify, transport=TRANSPORT)
        response = self._send(http, "POST", "/auth/token", json=self._login)
        if response.status_code not in (200, 201):
            # Airflow answers a wrong password with 401, so a 403 here most likely comes from
            # NGINX refusing an address outside the `[airflow] allow` list.
            hint = (
                "; this address may be outside `[airflow] allow` in vecten.toml"
                if response.status_code == 403
                else ""
            )
            raise SourceUnavailable(
                f"Airflow at {self.base_url} refused the login (HTTP {response.status_code})" + hint
            )
        http.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        self._http = http
        return http

    def _send(self, http: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return http.request(method, self.base_url + path, **kwargs)
        except httpx.HTTPError as exc:
            raise SourceUnavailable(
                f"Airflow at {self.base_url} is not reachable ({type(exc).__name__})"
            ) from None

    def _get(self, path: str, **kwargs) -> httpx.Response:
        response = self._send(self._client(), "GET", path, **kwargs)
        if response.status_code != 200:
            raise SourceUnavailable(
                f"Airflow at {self.base_url} answered HTTP {response.status_code} for {path}"
            )
        return response

    def task_instances(self, since: datetime) -> list[TaskAttempt]:
        rows: list[dict] = []
        total = None
        while total is None or len(rows) < total:
            params = {
                "run_after_gte": since.isoformat(),
                "order_by": "-run_after",
                "limit": PAGE,
                "offset": len(rows),
                "state": list(FINISHED),
            }
            page = self._get("/api/v2/dags/~/dagRuns/~/taskInstances", params=params).json()
            total = int(page.get("total_entries", 0))
            if total > MAX_ROWS:
                raise SourceUnavailable(
                    f"Airflow at {self.base_url} holds {total} task instances in the window, "
                    f"more than the {MAX_ROWS} read, so the scan would be incomplete"
                )
            batch = page.get("task_instances", [])
            if not batch:
                break
            rows += batch
        return [TaskAttempt.from_api(row) for row in rows]

    def log(self, attempt: TaskAttempt) -> str | None:
        path = (
            f"/api/v2/dags/{quote(attempt.dag_id, safe='')}/dagRuns/"
            f"{quote(attempt.run_id, safe='')}/taskInstances/{quote(attempt.task_id, safe='')}"
            f"/logs/{attempt.try_number}"
        )
        params = {"map_index": attempt.map_index, "full_content": "true"}
        response = self._send(
            self._client(), "GET", path, params=params, headers={"Accept": "application/json"}
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SourceUnavailable(
                f"Airflow at {self.base_url} answered HTTP {response.status_code} for a task log"
            )
        content = response.json().get("content", [])
        if isinstance(content, str):
            return content
        return "\n".join(
            json.dumps(item) if isinstance(item, dict) else str(item) for item in content
        )

    def log_path(self, attempt: TaskAttempt) -> str:
        return (
            f"{self.base_url}/api/v2/dags/{attempt.dag_id}/dagRuns/{attempt.run_id}/"
            f"taskInstances/{attempt.task_id}/logs/{attempt.try_number}"
            f"?map_index={attempt.map_index}"
        )


class ApiLogs:
    """Task logs read through the API, where the laptop stack keeps them."""

    def __init__(self, client: AirflowClient):
        self.client = client

    def read(self, attempt: TaskAttempt) -> str | None:
        return self.client.log(attempt)

    def location(self, attempt: TaskAttempt) -> str:
        return self.client.log_path(attempt)


def task_key(attempt: TaskAttempt) -> str:
    if is_sync_task(attempt.task_id) and attempt.map_index >= 0:
        return sync_task_key(attempt.rendered_map_index or str(attempt.map_index))
    return attempt.task_id


def _sync_summary(attempt: TaskAttempt) -> bool:
    return attempt.task_id in SUMMARY_TASKS and attempt.dag_id.startswith("sync_")


def _archived_nodes(ctx: IssueContext, attempt: TaskAttempt) -> list[str] | None:
    """The dbt issue ids of the nodes a `build` or `convention` attempt archived as failing;
    None when the attempt is no such task or archived nothing."""
    if attempt.dag_id != DBT_DAG or attempt.task_id not in DBT_RESULT_TASKS:
        return None
    if ctx.artifacts is None:
        return None
    body = ctx.artifacts.read(run_keys(attempt.run_id)[attempt.task_id])
    if body is None:
        return None
    try:
        results = json.loads(body).get("results") or []
    except ValueError, AttributeError:
        return None
    return sorted(
        {
            node_issue_id(r["unique_id"])
            for r in results
            if isinstance(r, dict) and r.get("unique_id") and r.get("status") in DBT_FAILED
        }
    )


def _failure(ctx: IssueContext, attempt: TaskAttempt) -> tuple[str, str, str]:
    """The class to group by, the error line and the redacted excerpt of one failed attempt."""
    text = ctx.logs.read(attempt) if ctx.logs is not None else None
    found = parse_log(text) if text else None
    if found is None:
        return NO_LOG_CLASS, "", ""
    error_class = failure_class(found.exc_type, found.exc_value)
    error = cap(
        redact(found.exc_value, ctx.secrets).splitlines()[0] if found.exc_value else "", ERROR_LIMIT
    )
    return error_class, error, cap(redact(found.excerpt, ctx.secrets), EXCERPT_LIMIT)


def _title(ctx: IssueContext, attempt: TaskAttempt, key: str, error_class: str) -> str:
    connector = sync_connector(ctx.config, attempt.dag_id)
    if connector is not None and key.startswith(sync_task_key("")):
        what = f"{connector.name} object {key.split('.', 1)[1]}"
    else:
        what = f"{attempt.dag_id} task {key}"
    return f"{what} fails with {error_class}"


def collect_airflow(ctx: IssueContext, *, summaries: bool = False) -> list[Issue]:
    """The open Airflow issues. A dbt `build` or `convention` failure whose results were
    archived is listed as its failing nodes instead; with `summaries` it is included too, so
    the id its alert posted still resolves, its evidence naming those node issues."""
    groups: dict[tuple[str, str], list[TaskAttempt]] = defaultdict(list)
    covered: dict[str, list[str]] = {}
    for attempt in ctx.attempts():
        if attempt.state not in FINISHED or _sync_summary(attempt):
            continue
        if attempt.state == FAILED:
            nodes = _archived_nodes(ctx, attempt)
            if nodes is not None:
                if not summaries:
                    continue
                covered[attempt.run_id + "/" + attempt.task_id] = nodes
        groups[(attempt.dag_id, task_key(attempt))].append(attempt)
    issues = []
    for (dag_id, key), attempts in sorted(groups.items()):
        failures = []
        for attempt in sorted(attempts, key=lambda a: a.run_after, reverse=True):
            if attempt.state in GREEN:
                break
            if attempt.state == FAILED:
                failures.append(attempt)
        by_class: dict[str, list[tuple[TaskAttempt, str, str]]] = defaultdict(list)
        for attempt in failures:
            error_class, error, excerpt = _failure(ctx, attempt)
            by_class[error_class].append((attempt, error, excerpt))
        for error_class, seen in sorted(by_class.items()):
            newest, error, excerpt = seen[0]
            route = airflow_resolution(ctx.config, ctx.env, dag_id, error_class, error)
            evidence = {
                "run_id": newest.run_id,
                "dag_id": dag_id,
                "task_id": newest.task_id,
                "map_index": newest.map_index,
                "try_number": newest.try_number,
                "error_class": error_class,
                "error": error,
                "excerpt": excerpt,
                "location": ctx.logs.location(newest) if ctx.logs is not None else "",
            }
            nodes = covered.get(newest.run_id + "/" + newest.task_id)
            if nodes is not None:
                evidence["covered_by"] = nodes
            issues.append(
                Issue(
                    id=airflow_issue_id(dag_id, key, error_class),
                    source="airflow",
                    kind=route.kind,
                    title=_title(ctx, newest, key, error_class) + route.detail,
                    resolution=route.resolution,
                    occurrences=len(seen),
                    first_seen=iso(seen[-1][0].run_after),
                    last_seen=iso(newest.run_after),
                    evidence=untrusted(evidence),
                )
            )
    return issues
