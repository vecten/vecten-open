"""The Docker calls `up` and `down` make, behind a protocol tests replace."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from vecten.stack.compose import STACK_DIR
from vecten_shared.output import ExitCode, Failure

# `docker info` hangs rather than failing while the Docker daemon is still starting.
INFO_TIMEOUT_SECONDS = 15


class DockerRunner(Protocol):
    def available(self) -> bool: ...

    def image_exists(self, tag: str) -> bool: ...

    def compose(
        self, project_root: Path, files: list[Path], profiles: list[str], args: list[str]
    ) -> subprocess.CompletedProcess: ...

    def volumes_by_label(self, project: str) -> list[str]: ...

    def remove_volumes(self, names: list[str]) -> subprocess.CompletedProcess: ...


class SubprocessDocker:
    def available(self) -> bool:
        if not shutil.which("docker"):
            return False
        try:
            info = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=INFO_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            return False
        return info.returncode == 0

    def image_exists(self, tag: str) -> bool:
        return (
            subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0
        )

    def compose(
        self, project_root: Path, files: list[Path], profiles: list[str], args: list[str]
    ) -> subprocess.CompletedProcess:
        # Relative mounts in the generated file resolve against the project directory, and
        # the secrets live in the project's .env rather than beside the generated file.
        cmd = [
            "docker",
            "compose",
            "--project-directory",
            str(project_root / STACK_DIR),
            "--env-file",
            str(project_root / ".env"),
        ]
        for f in files:
            cmd += ["-f", str(f)]
        for p in profiles:
            cmd += ["--profile", p]
        env = dict(os.environ)
        if "--build" in args:
            from vecten_shared.config import load_config
            from vecten_shared.provider.local import LocalProvider

            if load_config(project_root).packages.private_index:
                env["PACKAGE_INDEX_URL"] = LocalProvider(
                    project_root, inside_compose=False
                ).get_secret("PACKAGE_INDEX_URL")
        return subprocess.run(cmd + args, capture_output=True, text=True, env=env)

    def volumes_by_label(self, project: str) -> list[str]:
        label = f"label=com.docker.compose.project={project}"
        listing = subprocess.run(
            ["docker", "volume", "ls", "-q", "--filter", label], capture_output=True, text=True
        )
        if listing.returncode != 0:
            raise Failure(
                ExitCode.TOOL_FAILED, f"docker volume ls failed: {listing.stderr[-2000:]}"
            )
        return listing.stdout.split()

    def remove_volumes(self, names: list[str]) -> subprocess.CompletedProcess:
        # `docker volume rm` with no names is a usage error, not a no-op.
        if not names:
            return subprocess.CompletedProcess(["docker", "volume", "rm"], 0, "", "")
        return subprocess.run(["docker", "volume", "rm", *names], capture_output=True, text=True)
