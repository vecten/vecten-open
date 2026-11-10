from __future__ import annotations

import secrets
from pathlib import Path

from cryptography.fernet import Fernet

from vecten_shared.config import VectenConfig
from vecten_shared.envfile import read_env_values
from vecten_shared.provider.local import LocalProvider

GENERATED = (
    "AIRFLOW_DB_PASSWORD",
    "MCP_STATE_DB_PASSWORD",
    "LOADER_PASSWORD",
    "PROVISIONER_PASSWORD",
    "IMPERSONATOR_PASSWORD",
    "POSTGRES_MASTER_PASSWORD",
    "AIRFLOW_ADMIN_PASSWORD",
    "AIRFLOW_API_SECRET_KEY",
    "AIRFLOW_JWT_SECRET",
)
FIXED = {"AIRFLOW_ADMIN_USER": "admin"}
# An older laptop's .env still holds these; they stay the stack's, never offered for a cloud.
RETIRED = ("AIRFLOW_BASIC_AUTH_PASSWORD", "AIRFLOW_BASIC_AUTH_USER")
STACK_MANAGED: frozenset[str] = frozenset({*GENERATED, *FIXED, *RETIRED, "AIRFLOW_FERNET_KEY"})


def _read_env(env_file: Path) -> dict[str, str | None]:
    return read_env_values(env_file)


def ensure_local_env(project_root: Path, config: VectenConfig) -> dict[str, str]:
    provider = LocalProvider(project_root)
    current = _read_env(provider.env_file)
    for name in GENERATED:
        if not current.get(name):
            provider.put_secret(name, secrets.token_urlsafe(24))
    if not current.get("AIRFLOW_FERNET_KEY"):
        provider.put_secret("AIRFLOW_FERNET_KEY", Fernet.generate_key().decode())
    for name, value in FIXED.items():
        if not current.get(name):
            provider.put_secret(name, value)
    return {k: v for k, v in _read_env(provider.env_file).items() if v is not None}
