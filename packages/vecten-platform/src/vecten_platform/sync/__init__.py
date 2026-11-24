"""The sync runtime shared by `vecten connector run` and the Airflow sync DAGs."""

from __future__ import annotations

import os

# dlt reads this before anything else runs; the product never phones home.
os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

# Vecten supplies dlt configuration and secrets without consulting Airflow Variables.
os.environ.setdefault("PROVIDERS__ENABLE_AIRFLOW_SECRETS", "false")

from vecten_platform.sync.context import RuntimeContext, runtime, secret, set_runtime  # noqa: E402


class TerminalConnectorError(Exception):
    """Raise during an object load when another attempt cannot succeed."""


__all__ = ["RuntimeContext", "TerminalConnectorError", "runtime", "secret", "set_runtime"]
