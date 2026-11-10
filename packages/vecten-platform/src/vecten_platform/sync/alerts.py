"""Failure callback: one webhook post per failed task, when ALERT_WEBHOOK_URL is set."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

import httpx

from vecten_shared.issues import airflow_issue_id, failure_class, redact, sync_task_key

log = logging.getLogger(__name__)

ERROR_LIMIT = 200
SYNC_TASK = "sync"


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def _task_key(task_id: str, exc: BaseException | None) -> str:
    # The mapped sync task fails per object, and `vecten issues` keys it by the object name.
    object_name = getattr(exc, "object_name", None)
    return sync_task_key(object_name) if task_id == SYNC_TASK and object_name else task_id


def alert_payload(context: dict) -> dict:
    exc = context.get("exception")
    error_class = (
        failure_class(type(exc).__name__, first_line(str(exc))) if exc is not None else "TaskFailed"
    )
    ti = context.get("task_instance")
    dag_id = context["dag"].dag_id
    task_id = context["task"].task_id
    identifier = airflow_issue_id(dag_id, _task_key(task_id, exc), error_class)
    command = f"ask your agent: `vecten issues show {identifier}`"
    object_name = getattr(exc, "object_name", None)
    subject = f"{dag_id}.{task_id} failed" + (f" for {object_name}" if object_name else "")
    return {
        "dag_id": dag_id,
        "task_id": task_id,
        "run_id": context.get("run_id"),
        "map_index": getattr(ti, "map_index", -1),
        "try_number": getattr(ti, "try_number", None),
        "error_class": error_class,
        "error": redact(first_line(str(exc)))[:ERROR_LIMIT] if exc is not None else "",
        "issue_id": identifier,
        "message": command,
        # Chat webhooks such as Slack's and Mattermost's display `text` and ignore the rest.
        "text": (
            f"{subject} with {error_class} (run {context.get('run_id')}), "
            f"issue {identifier}; {command}"
        ),
    }


def _post(url: str, json: dict) -> None:
    httpx.post(url, json=json, timeout=10).raise_for_status()


def alert_on_failure(context: dict, post: Callable[[str, dict], None] | None = None) -> None:
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return
    try:
        (post or _post)(url, alert_payload(context))
    except Exception as exc:
        # The task already failed; the alert must not turn that into a second failure.
        # The webhook URL is itself a credential, and httpx errors repeat it.
        detail = redact(str(exc).replace(url, "[webhook url]"))
        log.warning("alert webhook failed: %s: %s", type(exc).__name__, detail)
