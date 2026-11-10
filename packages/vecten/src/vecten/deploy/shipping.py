"""What shipping a release means on every cloud: the host order, the bundle's key, and the
health answer each component must give before a ship counts as done."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from typing import NamedTuple

from vecten.stack.compose import vm_hostnames
from vecten.stack.nginx import AIRFLOW_HEALTH_PATH
from vecten_shared.issues import redact
from vecten_shared.provider.environment import EnvironmentOutputs

POLL_INTERVAL = 5
FETCH_TIMEOUT = 10
# The VM agent prefixes compose's last output and each failing container's log line with
# this; the lines quote what they printed, so they are redacted again here and capped.
AGENT_LOG_PREFIX = "vecten agent log: "
LOG_LINE_LIMIT = 300
LOG_LINES_MAX = 40
_UNPRINTABLE = re.compile(r"[^\x20-\x7e]")


def answers_200(status: int, body: str) -> bool:
    return status == 200


def airflow_healthy(status: int, body: str) -> bool:
    """Airflow 3 answers its health path with 200 and each component's state in the body.

    The scheduler's heartbeat may lag a fresh start beyond the poll window's first minutes,
    so only the metadatabase, which every Airflow request needs, decides.
    """
    if status != 200:
        return False
    try:
        doc = json.loads(body)
    except ValueError:
        return False
    database = doc.get("metadatabase") if isinstance(doc, dict) else None
    return isinstance(database, dict) and database.get("status") == "healthy"


class Health(NamedTuple):
    key: str
    path: str
    healthy: Callable[[int, str], bool]


# NGINX passes these paths to the application unauthenticated, so the answers are its own.
HEALTH = {
    "platform": Health("airflow", AIRFLOW_HEALTH_PATH, airflow_healthy),
    "mcp": Health("mcp", "/health", answers_200),
}


def ship_order(outputs: EnvironmentOutputs) -> list[str]:
    """The setup host first: it creates the credentials and databases the others connect to."""
    rest = sorted(h for h in outputs.hosts if h != outputs.setup_host)
    return [outputs.setup_host, *rest] if outputs.setup_host in outputs.hosts else rest


def host_for(outputs: EnvironmentOutputs, component: str) -> str | None:
    return next((h for h in ship_order(outputs) if component in outputs.hosts[h].components), None)


def bundle_key(host: str, commit: str) -> str:
    return f"bundles/{host}/{commit}.tar.gz"


def component_url(outputs: EnvironmentOutputs, component: str) -> str:
    health = HEALTH[component]
    return f"https://{vm_hostnames(outputs)[health.key]}{health.path}"


def fetch_response(url: str) -> tuple[int, str] | None:
    import httpx

    try:
        response = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=False)
    except httpx.HTTPError:
        return None
    return response.status_code, response.text


def poll(
    url: str,
    timeout: int,
    healthy: Callable[[int, str], bool] = answers_200,
    interval: int = POLL_INTERVAL,
    fetch: Callable[[str], tuple[int, str] | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True once `url` gives an answer `healthy` accepts within `timeout` seconds."""
    fetch = fetch or fetch_response
    deadline = clock() + timeout
    while True:
        answer = fetch(url)
        if answer is not None and healthy(*answer):
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def log_excerpt(lines: Iterable[object], known: Iterable[str]) -> list[str]:
    """The agent's container log lines, printable, redacted, each cut to 300 characters."""
    known = list(known)
    kept = []
    for raw in lines:
        if not isinstance(raw, str):
            continue
        line = redact(_UNPRINTABLE.sub(" ", raw).strip(), known)
        if line:
            kept.append(line[:LOG_LINE_LIMIT])
    return kept[:LOG_LINES_MAX]


def container_log_message(lines: list[str]) -> str:
    if not lines:
        return ""
    return "\nlast output of compose and the failing containers:\n" + "\n".join(
        "  " + x for x in lines
    )
