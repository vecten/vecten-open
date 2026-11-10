"""Connector names become schema names, package names and record names."""

from __future__ import annotations

import re

from vecten_shared.names import CONNECTOR_NAME, CONNECTOR_RULE, check_connector_name

__all__ = [
    "CONNECTOR_NAME",
    "CONNECTOR_RULE",
    "check_connector_name",
    "run_record_name",
    "secret_name",
]

RUN_RECORD = "connector-run-{name}"


def run_record_name(name: str) -> str:
    return RUN_RECORD.format(name=name)


def secret_name(connector: str, param: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{connector}_{param}").strip("_").upper()
