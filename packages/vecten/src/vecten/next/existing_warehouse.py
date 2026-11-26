"""The existing-warehouse items `next` reports for an MCP-only project, from vecten.toml only."""

from __future__ import annotations

from pathlib import Path

from vecten.next.items import Item
from vecten.secrets import provisioner_secret_names
from vecten_shared.config import EnvironmentFacts, VectenConfig, source_layer_areas
from vecten_shared.identity_setup import snowflake_access_version
from vecten_shared.provider import shared_layout
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
    if not script_runs(root, config, env):
        command = "vecten mcp provisioner-script" + (f" --env {env}" if config.deploy else "")
        items.append(Item("command", "Prepare the existing warehouse for the MCP", command))
        return items
    rerun = access_rerun_item(root, config, env)
    return [*items, rerun] if rerun else items


def script_runs(root: Path, config: VectenConfig, env: str) -> list[tuple[int, list[str]]]:
    """The access version each confirmed run of the provisioner script that counts for `env`
    installed, 1 where the run recorded none."""
    # Postgres grants on the one existing warehouse whichever environment the script was run
    # for. Snowflake's External OAuth names an issuer: `env`'s own, or prod's for the laptop.
    if config.warehouse.kind == "postgres":
        names = [*config.environments, "local"]
    else:
        names = [env, "local"] if env == "prod" else [env]
    runs = []
    for name in dict.fromkeys([env, *names]):
        facts = config.environments.get(name) or EnvironmentFacts()
        record = read_record(root, f"provisioner-{name}") or {}
        if facts.provisioner_script_run and (facts.access_version or not record.get("confirmed")):
            runs.append((facts.access_version or 1, facts.access_source_areas or []))
            continue
        if record.get("confirmed"):
            version = record.get("access_version")
            runs.append(
                (
                    version if isinstance(version, int) else 1,
                    record.get("access_source_areas") or [],
                )
            )
    return runs


def access_rerun_item(root: Path, config: VectenConfig, env: str) -> Item | None:
    """The provisioner script again while the access procedures it installed for `env` are
    older than the ones this release's MCP needs; only Snowflake's procedures carry a version."""
    if config.warehouse.kind != "snowflake" or not config.has("mcp"):
        return None
    runs = script_runs(root, config, env)
    if not runs:
        return None
    areas = source_layer_areas(config.areas)
    # The confirmed facts for this environment travel with the project. A stale laptop
    # record can describe an earlier installation from another checkout.
    version, installed = runs[0]
    if version >= snowflake_access_version(shared_layout(config, env), areas):
        if installed == areas:
            return None
        return Item(
            "command",
            f"Update the restricted source areas in {env}'s Snowflake procedures",
            f"vecten mcp provisioner-script --env {env}",
        )
    title = (
        f"Rerun the provisioner script for {env}: the access procedures it installed are "
        "older than this release's"
    )
    return Item("command", title, f"vecten mcp provisioner-script --env {env}")


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
