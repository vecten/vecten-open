import os
import random
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import cache
from pathlib import Path

import pytest

from vecten_shared.provider.local import LOCAL_PG_PUBLISHED_PORT
from vecten_shared.versions import VERSIONS

REPO = Path(__file__).resolve().parents[3]
# What the MCP Dockerfile reads to install the locked dependencies: `uv sync --locked` checks
# the lock against every workspace member's metadata.
LOCK_INPUTS = (
    "uv.lock",
    "pyproject.toml",
    *sorted(str(p.relative_to(REPO)) for p in REPO.glob("packages/*/pyproject.toml")),
)


def remove_stack_images(project: str) -> None:
    """Remove the images `vecten up` built for one compose project; base images stay cached.

    Each test project lives in a fresh temporary directory, so its compose project name, and
    with it every image it builds, is unique to the run and would otherwise accumulate.
    """
    listed = subprocess.run(
        [
            "docker",
            "image",
            "ls",
            "--filter",
            f"reference={project}-*",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    if listed:
        subprocess.run(["docker", "image", "rm", "-f", *listed], capture_output=True)


_built_mcp_image: str | None = None


def ensure_mcp_image() -> str:
    """The local MCP base image `vecten dev build-images` tags, built from this checkout.

    The tag names only the version, so an image left by an earlier run can hold other code;
    the first call in a run always builds, and Docker reuses the layers whose inputs are
    unchanged. The build context is a temporary directory holding `dist/` and the lock inputs,
    so the checkout's own `dist/` is never read or cleared, and the build sees no wheel of
    another version.
    """
    from vecten.stack.images import base_image
    from vecten_shared.versions import VERSIONS

    global _built_mcp_image
    if not docker_available():
        pytest.skip("Docker is not available")
    if _built_mcp_image is not None:
        return _built_mcp_image
    image = base_image("mcp", None)
    with tempfile.TemporaryDirectory(prefix="vecten-mcp-image-") as context:
        dist = Path(context) / "dist"
        for name in LOCK_INPUTS:
            (Path(context) / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, Path(context) / name)
        for package in ("vecten-shared", "vecten-mcp"):
            subprocess.run(
                ["uv", "build", str(REPO / "packages" / package), "--wheel", "--out-dir", dist],
                check=True,
                capture_output=True,
            )
        built = subprocess.run(
            ["docker", "build", "-f", str(REPO / "images/mcp/Dockerfile")]
            + ["--build-arg", f"BASE_IMAGE={VERSIONS.mcp_base_image}", "-t", image, context],
            capture_output=True,
            text=True,
        )
        if built.returncode:
            raise RuntimeError(f"building {image} failed:\n{built.stderr[-3000:]}")
    _built_mcp_image = image
    return image


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


def stack_pg_port() -> int:
    """A free port for a test stack's published Postgres, below the kernel's ephemeral range.

    The kernel hands any port in that range, 55432 included, to an outbound connection as its
    local end, and such a socket can make `vecten up` fail to publish the port. Linux's default
    range starts at 32768 and macOS's at 49152, so only a listener takes a port below both, and
    the bind here sees a listener.
    """
    while True:
        port = random.randrange(20000, 32768)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port


@contextmanager
def pg_container(init_dir, env: dict[str, str]):
    import psycopg

    name = f"vecten-test-pg-{uuid.uuid4().hex[:8]}"
    port = free_port()
    cmd = ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:5432"]
    cmd += ["-v", f"{init_dir}:/docker-entrypoint-initdb.d:ro"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [f"postgres:{VERSIONS.postgres_major}"]
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


def require_base_image(kind: str) -> str:
    """The local base image `vecten dev build-images` tags; skipped on a laptop without it.

    In CI a missing image fails the test, because the job that runs it builds the images and a
    skip there would pass without testing anything.
    """
    from vecten.stack.images import base_image

    if not docker_available():
        pytest.skip("Docker is not available")
    image = base_image(kind, None)
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        message = f"{image} is missing; build it with vecten dev build-images"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return image
