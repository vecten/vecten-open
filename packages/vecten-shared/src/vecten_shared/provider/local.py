"""The laptop: secrets from .env, one Postgres container for every database."""

from __future__ import annotations

import os
import re
from pathlib import Path

from vecten_shared.config import load_config, stack_project_name
from vecten_shared.envfile import read_env_text, read_env_values
from vecten_shared.names import check_secret_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import (
    ROLE_FOR_DB,
    ConnectionSpec,
    DbName,
    LogTarget,
    check_db_name,
    register_provider,
)
from vecten_shared.records import write_text_atomic

LOCAL_PG_PUBLISHED_PORT = 55432
# Moves the published port for stacks that must not meet on 55432, such as the test suite's.
# `up` and every host-side command read it, so the same value must reach each of them.
LOCAL_PG_PORT_ENV = "VECTEN_LOCAL_PG_PORT"


def local_pg_published_port() -> int:
    value = os.environ.get(LOCAL_PG_PORT_ENV)
    if not value:
        return LOCAL_PG_PUBLISHED_PORT
    if not value.isdigit() or not 0 < int(value) < 65536:
        raise Failure(ExitCode.USAGE, f"{LOCAL_PG_PORT_ENV} must be a TCP port, not {value!r}")
    return int(value)


_BARE_VALUE = re.compile(r"[A-Za-z0-9_./:@+=,\-]*")


def _env_value(name: str, value: str) -> str:
    check_secret_name(name)
    if _BARE_VALUE.fullmatch(value):
        return value
    if "'" in value or not value.isprintable():
        raise Failure(
            ExitCode.USAGE,
            f"secret {name} contains a character the local .env cannot store "
            "(newline or single quote); choose another value",
        )
    # python-dotenv unescapes \\ and \' inside single quotes, so either would not read back.
    if "\\\\" in value or value.endswith("\\"):
        raise Failure(
            ExitCode.USAGE,
            f"secret {name} contains a double backslash or ends in a backslash, which the "
            "local .env cannot store; choose another value",
        )
    # Compose expands $ inside double quotes, so single quotes are the only
    # literal form that python-dotenv (interpolation off) and Compose both read.
    return f"'{value}'"


def _assigns(line: str, name: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    key, sep, _ = stripped.partition("=")
    return bool(sep) and key.strip() == name


class LocalProvider:
    def __init__(self, project_root: Path, inside_compose: bool = True):
        self.project_root = project_root
        self.inside_compose = inside_compose
        self.env_file = project_root / ".env"

    def bootstrap(self, env: str, repository: str) -> None:
        return None

    def provision(self, env: str) -> dict:
        return {}

    def destroy(self, env: str) -> None:
        return None

    def ship(self, env: str, component: str, project_dir: Path) -> None:
        return None

    def _values(self) -> dict[str, str | None]:
        return read_env_values(self.env_file)

    def get_secret(self, name: str) -> str:
        # Inside the Compose stack the stack passes each secret as an environment variable;
        # the .env file stays on the host.
        value = os.environ.get(name) if self.inside_compose else self._values().get(name)
        if value is None:
            raise Failure(
                ExitCode.PREREQUISITE, f"secret {name} is not set; run `vecten secret set {name}`"
            )
        if not value:
            where = "the environment" if self.inside_compose else ".env"
            raise Failure(
                ExitCode.PREREQUISITE,
                f"secret {name} is empty in {where}; run `vecten secret set {name}`",
            )
        return value

    def list_secrets(self) -> list[str]:
        return sorted(self._values())

    def put_secret(self, name: str, value: str) -> None:
        assignment = f"{name}={_env_value(name, value)}"
        # Read before touching: an unreadable .env must stop the write, not be rewritten
        # with only this secret.
        text = read_env_text(self.env_file)
        # .env holds secrets: it is 0600 before any byte lands and after every rewrite.
        self.env_file.touch(mode=0o600, exist_ok=True)
        lines: list[str] = []
        replaced = False
        for line in (text or "").splitlines():
            if _assigns(line, name):
                if not replaced:
                    lines.append(assignment)
                    replaced = True
                continue
            lines.append(line)
        if not replaced:
            lines.append(assignment)
        write_text_atomic(self.env_file, "\n".join(lines) + "\n", mode=0o600)

    def connection(self, env: str, db: DbName) -> ConnectionSpec:
        check_db_name(db)
        return ConnectionSpec(
            kind="postgres",
            database=db,
            role=ROLE_FOR_DB[db],
            auth="password",
            host="postgres" if self.inside_compose else "localhost",
            port=5432 if self.inside_compose else local_pg_published_port(),
        )

    def log_target(self, env: str) -> LogTarget:
        name = stack_project_name(self.project_root, load_config(self.project_root).project.name)
        return LogTarget(kind="docker_volume", location=f"{name}_airflow-logs")


# The laptop has one environment, so the bound name changes nothing here.
register_provider("local", lambda project_root, env: LocalProvider(project_root))
