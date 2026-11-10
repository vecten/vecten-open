"""Which skill, command or question resolves a platform failure.

A sync failure is code to fix unless its error says otherwise: a missing or refused
credential is fixed by storing a new one, and an upstream outage by waiting it out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from vecten_shared.config import ConnectorDecl, VectenConfig
from vecten_shared.issues import IssueKind

DBT_DAG = "dbt_build"
LOCAL_ENV = "local"
_MISSING_SECRET = re.compile(r"\bsecret ([A-Z][A-Z0-9_]*) is not set\b")
_AUTH = re.compile(
    r"\b(?:401|403)\b|unauthori[sz]ed|forbidden|invalid[ _-]?(?:api[ _-]?key|token|grant)"
    r"|authenticat|access denied|expired (?:token|key)",
    re.IGNORECASE,
)
_OUTAGE_CLASSES = {
    "ConnectionError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "Timeout",
    "TimeoutError",
    "TimeoutException",
    "RemoteDisconnected",
    "ProtocolError",
}
_OUTAGE = re.compile(
    r"\b(?:429|500|502|503|504)\b|service unavailable|bad gateway|gateway time-?out"
    r"|too many requests|timed out|temporarily unavailable",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Resolution:
    kind: IssueKind
    resolution: str
    detail: str = ""
    """Added to the issue's title: what the agent needs to act on the resolution."""


def sync_connector(config: VectenConfig, dag_id: str) -> ConnectorDecl | None:
    """The connector whose sync DAG `dag_id` is, by the DAG naming `sync_<connector>_<group>`."""
    for connector in config.connectors:
        if any(dag_id == f"sync_{connector.name}_{group}" for group in connector.groups):
            return connector
    return None


def secret_set(name: str, env: str) -> str:
    return f"vecten secret set {name}" + ("" if env == LOCAL_ENV else f" --env {env}")


def _sync_resolution(connector: ConnectorDecl, env: str, error_class: str, error: str):
    missing = _MISSING_SECRET.search(error)
    if missing:
        return Resolution("command", secret_set(missing.group(1), env))
    if _AUTH.search(error) and connector.secrets:
        if len(connector.secrets) == 1:
            return Resolution("command", secret_set(connector.secrets[0], env))
        names = ", ".join(sorted(connector.secrets))
        return Resolution(
            "command",
            secret_set("<NAME>", env),
            f"; the source refused a credential, one of {names}",
        )
    if error_class in _OUTAGE_CLASSES or _OUTAGE.search(error):
        return Resolution(
            "ask",
            f"The {connector.name} source is failing upstream ({error_class}); nothing in the "
            "project needs to change. Tell the human, wait for the source to recover, and check "
            "again: the issue clears on the next green run.",
        )
    return Resolution("skill", "connector")


def airflow_resolution(
    config: VectenConfig, env: str, dag_id: str, error_class: str, error: str
) -> Resolution:
    connector = sync_connector(config, dag_id)
    if connector is not None:
        return _sync_resolution(connector, env, error_class, error)
    if dag_id == DBT_DAG:
        return Resolution("skill", "model")
    return Resolution(
        "ask",
        f"{dag_id} is not a DAG vecten renders; ask the human who owns it to look at the run",
    )
