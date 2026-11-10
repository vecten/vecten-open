"""Warehouses dbt builds against without credentials: a disposable Postgres and the emulation."""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vecten_platform.dbt.profile import profile_env
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import ConnectionSpec
from vecten_shared.versions import VERSIONS

PG_USER = "vecten"
DEFAULT_DATABASE = "warehouse"
PG_READY_SECONDS = 90
CONTAINER_ENV = "VECTEN_LOCAL_PG_CONTAINER"


@dataclass(frozen=True)
class LocalTarget:
    kind: Literal["postgres", "snowflake"]
    spec: ConnectionSpec
    password: str
    env: dict[str, str]
    in_process: bool
    workdir: Path


def _require_docker() -> None:
    if shutil.which("docker") is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            "Docker is needed for a local Postgres warehouse; install Docker and start it",
        )
    info = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if info.returncode != 0:
        raise Failure(
            ExitCode.PREREQUISITE,
            "Docker is needed for a local Postgres warehouse; the Docker daemon is not reachable",
        )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _answers(port: int, password: str, database: str) -> bool:
    import psycopg2

    try:
        conn = psycopg2.connect(
            host="127.0.0.1",
            port=port,
            user=PG_USER,
            password=password,
            dbname=database,
            connect_timeout=3,
        )
    except psycopg2.OperationalError:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("select 1")
            return cur.fetchone() == (1,)
    finally:
        conn.close()


def _wait_ready(name: str, port: int, password: str, database: str) -> None:
    deadline = time.monotonic() + PG_READY_SECONDS
    while True:
        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if running != "true":
            raise Failure(ExitCode.TOOL_FAILED, f"the local Postgres exited:\n{_logs(name)}")
        ready = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-U", PG_USER, "-d", database],
            capture_output=True,
            check=False,
        )
        # The entrypoint's init server listens on the socket only, so a TCP answer is the
        # final server.
        if ready.returncode == 0 and _answers(port, password, database):
            return
        if time.monotonic() > deadline:
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"the local Postgres was not ready within {PG_READY_SECONDS}s:\n{_logs(name)}",
            )
        time.sleep(1)


def _logs(name: str) -> str:
    out = subprocess.run(
        ["docker", "logs", "--tail", "40", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    ).stdout
    return out or ""


@contextmanager
def disposable_postgres(
    project_name: str, *, database: str = DEFAULT_DATABASE, workdir: Path | None = None
) -> Iterator[LocalTarget]:
    _require_docker()
    name = f"vecten-dbt-local-{uuid.uuid4().hex[:8]}"
    password = secrets.token_urlsafe(24)
    port = _free_port()
    cmd = ["docker", "run", "-d", "--name", name, "--label", f"vecten.project={project_name}"]
    cmd += ["-p", f"127.0.0.1:{port}:5432", "-e", f"POSTGRES_USER={PG_USER}"]
    # A bare `-e NAME` takes the value from the client's environment, off the command line.
    cmd += ["-e", "POSTGRES_PASSWORD", "-e", f"POSTGRES_DB={database}"]
    cmd += [f"postgres:{VERSIONS.postgres_major}"]
    spec = ConnectionSpec(
        kind="postgres",
        database=database,
        role=PG_USER,
        auth="password",
        host="127.0.0.1",
        port=port,
    )
    env = {**profile_env(spec, password, threads=4), CONTAINER_ENV: name}
    with tempfile.TemporaryDirectory(prefix="vecten-dbt-local-") as scratch:
        try:
            started = subprocess.run(
                cmd,
                env={**os.environ, "POSTGRES_PASSWORD": password},
                capture_output=True,
                text=True,
                check=False,
            )
            if started.returncode != 0:
                raise Failure(
                    ExitCode.TOOL_FAILED,
                    f"docker run postgres failed: {started.stderr.strip()[-2000:]}",
                )
            _wait_ready(name, port, password, database)
            yield LocalTarget(
                kind="postgres",
                spec=spec,
                password=password,
                env=env,
                in_process=False,
                workdir=workdir or Path(scratch),
            )
        finally:
            subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False)


@contextmanager
def snowflake_emulation(
    workdir: Path, *, database: str = DEFAULT_DATABASE
) -> Iterator[LocalTarget]:
    import fakesnow

    # Every emulation starts empty, so an earlier build's raw tables and dlt state never leak in.
    db_path = workdir / "fakesnow"
    for stale in (db_path, workdir / "dlt"):
        shutil.rmtree(stale, ignore_errors=True)
    db_path.mkdir(parents=True)
    # Snowflake folds unquoted names to upper case, and fakesnow keeps each database in a file
    # named after the folded name.
    spec = ConnectionSpec(
        kind="snowflake",
        database=database.upper(),
        role="LOADER",
        auth="password",
        account="fakesnow",
        warehouse="LOCAL_WH",
    )
    # fakesnow attaches one DuckDB file per database per connection; parallel dbt threads
    # collide on that attach.
    env = {**profile_env(spec, "fakesnow", threads=1), "DBT_THREADS": "1"}
    # fakesnow transpiles every statement through sqlglot, which warns on each Snowflake command
    # it passes through untranslated; those warnings say nothing about the project.
    sqlglot_log = logging.getLogger("sqlglot")
    level = sqlglot_log.level
    sqlglot_log.setLevel(logging.ERROR)
    try:
        with fakesnow.patch(db_path=db_path):
            yield LocalTarget(
                kind="snowflake",
                spec=spec,
                password="fakesnow",
                env=env,
                in_process=True,
                workdir=workdir,
            )
    finally:
        sqlglot_log.setLevel(level)


@contextmanager
def local_target(config: VectenConfig, workdir: Path) -> Iterator[LocalTarget]:
    """The build database carries the deployed name, so generated sources match the build."""
    from vecten_shared.osi.generate import dataset_database

    workdir.mkdir(parents=True, exist_ok=True)
    database = dataset_database(config)
    if config.warehouse.kind == "snowflake":
        with snowflake_emulation(workdir, database=database) as target:
            yield target
    else:
        with disposable_postgres(config.project.name, database=database, workdir=workdir) as target:
            yield target
