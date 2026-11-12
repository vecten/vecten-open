"""Settings and declared secrets for extension and project code, in every runtime alike.

DAG code and connectors in Airflow, tools in the MCP server and connector runs in the CLI's
own process all read through `extension(<name>)` or `project()`. A handle serves only the
secrets its extension's manifest, or the project, declared, and only those delivered to the
component reading them, so a read fails the same way on the laptop and on any VM.

Compose sets `VECTEN_COMPONENT` and passes declared secrets as environment variables; outside
Compose the reader is the CLI's process, which acts as `platform` and reads `.env`.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from typing import Any

from vecten_shared.config import VectenConfig, load_config
from vecten_shared.declarations import (
    LOCAL,
    PROJECT,
    check_settings,
    component_for,
    effective_settings,
)
from vecten_shared.extension_record import RECORD_FILE, Extension, read_record
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import Provider, get_provider
from vecten_shared.provider.local import LocalProvider
from vecten_shared.requirements import parse_requirement
from vecten_shared.versions import normalize_name

COMPONENT_VAR = "VECTEN_COMPONENT"
_RECEIVERS = {"mcp": "the MCP server", "platform": "Airflow and the CLI"}


class Handle:
    """One extension's, or the project's, settings and secrets in this process's environment."""

    def __init__(
        self,
        owner: str,
        declared: tuple[str, ...],
        settings: dict[str, Any],
        env: str,
        component: str,
        root: Path,
    ) -> None:
        self._owner = owner
        self._declared = frozenset(declared)
        self.settings = MappingProxyType(settings)
        self.env = env
        self._component = component
        self._root = root
        self._provider: Provider | None = None

    def secret(self, name: str) -> str:
        if name not in self._declared:
            raise Failure(
                ExitCode.REFUSED,
                f"{self._owner} does not declare the secret {name}; declare it in "
                + ("its manifest" if self._owner != PROJECT else "vecten.toml's secrets"),
            )
        receiver = component_for(name)
        if receiver != self._component:
            raise Failure(
                ExitCode.REFUSED,
                f"{name} is delivered to {_RECEIVERS[receiver]}, not to the {self._component} "
                "component; declare a name for this component too",
            )
        if self._provider is None:
            self._provider = _provider(self._root, self.env)
        return self._provider.get_secret(name)


def _provider(root: Path, env: str) -> Provider:
    name = os.environ.get("VECTEN_PROVIDER") or "local"
    if name == "local":
        return LocalProvider(root, inside_compose=bool(os.environ.get(COMPONENT_VAR)))
    return get_provider(name, root, env)


def _root() -> Path:
    configured = os.environ.get("VECTEN_PROJECT_ROOT")
    if configured:
        return Path(configured)
    here = Path.cwd()
    for candidate in (here, *here.parents):
        if (candidate / "vecten.toml").is_file():
            return candidate
    raise Failure(ExitCode.PREREQUISITE, "no vecten.toml here or above; set VECTEN_PROJECT_ROOT")


def _loaded() -> tuple[Path, VectenConfig, dict[str, Extension], str, str]:
    root = _root()
    config = load_config(root)
    record = read_record(root)
    if record is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"{RECORD_FILE} is missing; settings and secrets are available after `vecten lock` "
            "and the next ship",
        )
    env = os.environ.get("VECTEN_ENV") or LOCAL
    component = os.environ.get(COMPONENT_VAR) or "platform"
    check_settings(config, record, env)
    return root, config, record, env, component


def extension(name: str) -> Handle:
    root, config, record, env, component = _loaded()
    wanted = normalize_name(name)
    listed = {normalize_name(r.name) for r in map(parse_requirement, config.extensions) if r}
    if wanted not in listed:
        raise Failure(ExitCode.CONFIG, f"extension {name} is not listed in vecten.toml")
    entry = record.get(wanted)
    secrets = entry.secrets if entry is not None else ()
    settings = effective_settings(config, record, wanted, env)
    return Handle(wanted, secrets, settings, env, component, root)


def project() -> Handle:
    root, config, record, env, component = _loaded()
    settings = effective_settings(config, record, PROJECT, env)
    return Handle(PROJECT, tuple(config.secrets), settings, env, component, root)
