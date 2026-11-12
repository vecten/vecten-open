"""Secrets the project or its extensions declare that are not set yet, locally or per environment.

Reads `vecten.toml`, `extensions.lock.json`, `.env` and the `secret set --env` records only.
"""

from __future__ import annotations

from pathlib import Path

from vecten.next.items import Item
from vecten.secrets import recorded_secret_names
from vecten_shared.config import VectenConfig
from vecten_shared.declarations import component_for
from vecten_shared.envfile import read_env_values
from vecten_shared.extension_record import read_record
from vecten_shared.provider.local import LocalProvider


def _declared(root: Path, config: VectenConfig) -> dict[str, str]:
    """Each delivered declared name, and who declares it."""
    owners = {name: "the project" for name in config.secrets}
    for extension, entry in sorted((read_record(root) or {}).items()):
        for name in entry.secrets:
            owners.setdefault(name, extension)
    return {n: o for n, o in sorted(owners.items()) if config.has(component_for(n))}


def declared_secret_items(root: Path, config: VectenConfig, env: str | None = None) -> list[Item]:
    """Locally (`env` None), names missing from `.env`; for `env`, names it has not received.

    A name set locally is left to the deploy step that copies local secrets to `env`.
    """
    values = read_env_values(LocalProvider(root, inside_compose=False).env_file)
    local = {name for name, value in values.items() if value}
    items = []
    for name, owner in _declared(root, config).items():
        if env is None and name not in local:
            items.append(
                Item("command", f"Set {name}, which {owner} declares", f"vecten secret set {name}")
            )
        elif env is not None and name not in local and name not in recorded_secret_names(root, env):
            items.append(
                Item(
                    "command",
                    f"Set {name} in {env}, which {owner} declares",
                    f"vecten secret set {name} --env {env}",
                )
            )
    return items
