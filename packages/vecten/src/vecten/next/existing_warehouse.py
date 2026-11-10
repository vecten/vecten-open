"""The existing-warehouse items `next` reports for an MCP-only project, from vecten.toml only."""

from __future__ import annotations

from pathlib import Path

from vecten.next.items import Item
from vecten.secrets import provisioner_secret_names
from vecten_shared.config import EnvironmentFacts, VectenConfig
from vecten_shared.provider.local import LocalProvider
from vecten_shared.records import read_record


def existing_warehouse_items(root: Path, config: VectenConfig, env: str) -> list[Item]:
    """The local provisioner credentials, then the administrator's run of the setup SQL."""
    if config.has("platform"):
        return []
    items = []
    present = set(LocalProvider(root, inside_compose=False).list_secrets())
    missing = [name for name in provisioner_secret_names(config) if name not in present]
    # On Snowflake the provisioner script generates the provisioner's key pair itself.
    if missing and config.warehouse.kind == "postgres":
        command = " && ".join(f"vecten secret set {name}" for name in missing)
        items.append(Item("command", "Store the provisioner credentials locally", command))
    # The script names the deployed issuer, which only the deploy layer gives; Snowflake's
    # External OAuth cannot do without it, so its script waits for the deploy questions.
    if config.deploy is None and config.warehouse.kind == "snowflake":
        return items
    if not _script_confirmed(root, config, env):
        command = "vecten mcp provisioner-script" + (f" --env {env}" if config.deploy else "")
        items.append(Item("command", "Prepare the existing warehouse for the MCP", command))
    return items


def _script_confirmed(root: Path, config: VectenConfig, env: str) -> bool:
    facts = config.environments.get(env) or EnvironmentFacts()
    # Every run grants on the one existing warehouse, so a run confirmed for either counts.
    return facts.provisioner_script_run or any(
        (read_record(root, f"provisioner-{name}") or {}).get("confirmed") for name in (env, "local")
    )


def provisioner_secrets_item(config: VectenConfig, env: str) -> Item | None:
    """The cloud copy of the MCP's warehouse logins, which `secret set --env` stores."""
    facts = config.environments.get(env) or EnvironmentFacts()
    if config.has("platform") or facts.provisioner_secrets_set:
        return None
    # The setup SQL gave each login one password, so the cloud copies the local value.
    command = " && ".join(
        f"vecten secret set {name} --env {env} --from-local"
        for name in provisioner_secret_names(config)
    )
    return Item("command", "Store the provisioner credentials", command)
