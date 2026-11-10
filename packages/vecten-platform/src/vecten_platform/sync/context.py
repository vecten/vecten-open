"""Where the runtime is: project files, provider, environment, dlt working directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import Provider, get_provider
from vecten_shared.provider.local import LocalProvider


@dataclass(frozen=True)
class RuntimeContext:
    project_root: Path
    connectors_dir: Path
    env: str
    provider: Provider
    dlt_dir: Path


_RUNTIME: RuntimeContext | None = None


def set_runtime(ctx: RuntimeContext | None) -> None:
    global _RUNTIME
    _RUNTIME = ctx


def _from_environment() -> RuntimeContext:
    root = os.environ.get("VECTEN_PROJECT_ROOT")
    if not root:
        raise Failure(
            ExitCode.PREREQUISITE,
            "VECTEN_PROJECT_ROOT is not set; the sync runtime needs the project root",
        )
    project_root = Path(root)
    env = os.environ.get("VECTEN_ENV") or "local"
    provider_name = os.environ.get("VECTEN_PROVIDER") or "local"
    inside_compose = os.environ.get("VECTEN_INSIDE_COMPOSE") == "1"
    provider: Provider
    if provider_name == "local":
        provider = LocalProvider(project_root, inside_compose=inside_compose)
    else:
        provider = get_provider(provider_name, project_root, env)
    connectors = os.environ.get("VECTEN_CONNECTORS_DIR")
    dlt_dir = os.environ.get("VECTEN_DLT_DIR")
    return RuntimeContext(
        project_root=project_root,
        connectors_dir=Path(connectors) if connectors else project_root / "connectors",
        env=env,
        provider=provider,
        dlt_dir=Path(dlt_dir) if dlt_dir else project_root / ".vecten" / "dlt",
    )


def runtime() -> RuntimeContext:
    return _RUNTIME if _RUNTIME is not None else _from_environment()


def secret(name: str) -> str:
    """The one way connector code reads a credential."""
    return runtime().provider.get_secret(name)
