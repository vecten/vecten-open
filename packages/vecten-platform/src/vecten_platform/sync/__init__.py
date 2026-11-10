"""The sync runtime shared by `vecten connector run` and the Airflow sync DAGs."""

from __future__ import annotations

import os

# dlt reads this before anything else runs; the product never phones home.
os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")

from vecten_platform.sync.context import RuntimeContext, runtime, secret, set_runtime  # noqa: E402

__all__ = ["RuntimeContext", "runtime", "secret", "set_runtime"]
