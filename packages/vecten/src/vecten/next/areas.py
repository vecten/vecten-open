"""The restricted-area items `next` reports, from vecten.toml only."""

from __future__ import annotations

from pathlib import Path

from vecten.next.items import Item
from vecten_shared.config import VectenConfig


def known_groups(config: VectenConfig) -> tuple[str, ...]:
    """`default` and every group a connector's or an area's access already names."""
    declared = [*config.connectors, *config.areas.values()]
    groups = {g for decl in declared if decl.access for g in decl.access.groups}
    return ("default", *sorted(groups - {"default"}))


def area_access_ask_items(config: VectenConfig) -> list[Item]:
    return [
        Item(
            kind="ask",
            title=f"Who may read the {name} area?",
            resolution=(
                f"Which groups may read the {name} area (schemas core_{name} and marts_{name})? "
                "Name one or more groups, comma-separated."
            ),
            answer_keys=(f"access.{name}",),
            allowed=known_groups(config),
        )
        for name, area in sorted(config.areas.items())
        if area.access is None or not area.access.groups
    ]


def area_access_apply_items(project_root: Path, config: VectenConfig, env: str) -> list[Item]:
    """The command seeding each declared area's grant into the deployed MCP's state; once
    recorded there the grant is the state's, so a later revoke in the panel stands."""
    facts = config.environments.get(env)
    if facts is None or not facts.deployed_commit:
        return []
    items: list[Item] = []
    for name, area in sorted(config.areas.items()):
        if not area.access or not area.access.groups or name in facts.area_access_applied:
            continue
        commands = []
        for group in sorted(area.access.groups):
            commands.append(f"vecten group add {group} --env {env}")
            commands.append(f"vecten grant --area {name} --group {group} --env {env}")
        items.append(
            Item(
                "command",
                f"Grant {', '.join(sorted(area.access.groups))} access to the {name} area",
                " && ".join(commands),
            )
        )
    return items
