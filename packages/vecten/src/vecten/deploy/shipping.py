"""What shipping a release means on every cloud: the host order, the bundle's key, and the
health answer each component must give before a ship counts as done."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from typing import NamedTuple

from vecten.stack.compose import service_host_name, vm_hostnames
from vecten.stack.nginx import AIRFLOW_HEALTH_PATH, SERVICE_HEALTH_PATH
from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import Extension
from vecten_shared.issues import redact
from vecten_shared.provider.environment import EnvironmentOutputs
from vecten_shared.services import bound_services

POLL_INTERVAL = 5
# How long one service may take to answer healthy; it bounds the ship, never fails it.
SERVICE_WINDOW = 60
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


HEALTHY, UNHEALTHY, WAITING, NOT_ROUTED, SERVED = (
    "healthy",
    "unhealthy",
    "waiting for deploy",
    "not routed",
    "served",
)


def service_healthy(status: int, body: str) -> bool:
    # The Compose check is `curl --fail`, which passes every answer below 400.
    return 200 <= status < 400


def _listed(outputs: EnvironmentOutputs, component: str, name: str) -> bool:
    host = host_for(outputs, component)
    return host is not None and name in outputs.hosts[host].service_hostnames


def unserved_hostnames(
    config: VectenConfig, record: dict[str, Extension], outputs: EnvironmentOutputs
) -> list[str]:
    """Bound service hostnames with no record on the host that runs their service."""
    hosts = vm_hostnames(outputs)
    names = (
        (service, service_host_name("vm", service.binding.hostname, hosts))
        for service in bound_services(config, record)
        if service.binding.hostname is not None
    )
    return sorted(name for service, name in names if not _listed(outputs, service.host, name))


def service_report(
    config: VectenConfig, record: dict[str, Extension], outputs: EnvironmentOutputs
) -> dict:
    """Each bound service's state once the hosts are shipped, and the routes whose hostname
    has no record yet; empty when nothing is bound. A state never fails the ship."""
    bound = bound_services(config, record)
    if not bound:
        return {}
    hosts = vm_hostnames(outputs)
    states: dict[str, str] = {}
    waiting: list[str] = []
    for service in bound:
        label = service.binding.hostname
        if label is None:
            states[service.key] = NOT_ROUTED
            continue
        name = service_host_name("vm", label, hosts)
        if not _listed(outputs, service.host, name):
            states[service.key] = WAITING
            waiting += [f"{service.key}.{route}" for route in service.routes]
            continue
        if service.service.health_path is None:
            states[service.key] = SERVED
            continue
        url = f"https://{name}{SERVICE_HEALTH_PATH}"
        healthy = poll(url, SERVICE_WINDOW, healthy=service_healthy)
        states[service.key] = HEALTHY if healthy else UNHEALTHY
    return {"services": states, "waiting_routes": waiting}
