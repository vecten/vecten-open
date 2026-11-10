import shutil
import socket
import subprocess
import time
import uuid
from contextlib import contextmanager
from functools import cache

from vecten_shared.provider.local import LOCAL_PG_PUBLISHED_PORT
from vecten_shared.versions import VERSIONS


# Every test module asks at import, and the daemon does not come or go during a run.
@cache
def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def free_port() -> int:
    """A port the kernel reports free, never the laptop stack's Postgres port: the kernel's
    ephemeral range holds it, and a later `vecten up` publishes it."""
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != LOCAL_PG_PUBLISHED_PORT:
            return port


@contextmanager
def pg_container(init_dir, env: dict[str, str], command: tuple[str, ...] | list[str] = ()):
    """A disposable Postgres; `command` replaces the image's command, as Compose's does."""
    import psycopg

    name = f"vecten-test-pg-{uuid.uuid4().hex[:8]}"
    port = free_port()
    cmd = ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:5432"]
    cmd += ["-v", f"{init_dir}:/docker-entrypoint-initdb.d:ro"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [f"postgres:{VERSIONS.postgres_major}", *command]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        deadline = time.time() + 90
        while True:
            logs = subprocess.run(
                ["docker", "logs", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            ).stdout
            running = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if running != "true":
                raise RuntimeError(f"postgres container exited during init:\n{logs}")
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True
            )
            # The entrypoint's temporary init server logs "ready to accept" first; only the
            # second occurrence is the server listening on TCP after the init scripts ran.
            if ready.returncode == 0 and logs.count("ready to accept connections") >= 2:
                break
            if time.time() > deadline:
                raise RuntimeError(logs)
            time.sleep(1)

        def connect(user: str, password: str, dbname: str):
            return psycopg.connect(
                host="127.0.0.1",
                port=port,
                user=user,
                password=password,
                dbname=dbname,
                connect_timeout=5,
            )

        yield connect, name
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
