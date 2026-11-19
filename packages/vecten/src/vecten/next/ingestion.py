"""The connector items `next` reports, from project files and run records only."""

from __future__ import annotations

from pathlib import Path

from vecten.connectors.names import run_record_name
from vecten.next.areas import known_groups
from vecten.next.items import Item
from vecten_platform.sync.fixtures import fixture_files
from vecten_shared.config import ConnectorDecl, VectenConfig
from vecten_shared.records import read_record


def access_ask_item(config: VectenConfig, decl: ConnectorDecl) -> Item:
    return Item(
        kind="ask",
        title=f"Who may read the {decl.name} raw data?",
        resolution=(
            f"Which groups may read the {decl.name} raw data (schema raw_{decl.name})? "
            "Name one or more groups, comma-separated."
        ),
        answer_keys=(f"access.{decl.name}",),
        allowed=known_groups(config),
    )


def ingestion_items(project_root: Path, config: VectenConfig) -> list[Item]:
    items: list[Item] = []
    declared = {c.name for c in config.connectors}
    for source in config.wanted:
        if source not in declared:
            items.append(Item("skill", f"Connect {source}", "connector"))
    present = [
        c
        for c in config.connectors
        if (project_root / "connectors" / c.name / "connector.py").is_file()
    ]
    for decl in config.connectors:
        if decl not in present:
            items.append(
                Item(
                    "command",
                    f"Add the {decl.name} connector files",
                    f"vecten connector add {decl.name}",
                )
            )
    for decl in present:
        if decl.access is None or not decl.access.groups:
            items.append(access_ask_item(config, decl))
    run_items: list[Item] = []
    fixture_items: list[Item] = []
    for decl in present:
        record = read_record(project_root, run_record_name(decl.name))
        has_fixtures = bool(fixture_files(project_root / "connectors" / decl.name / "fixtures"))
        if not record or not record.get("ok"):
            command = f"vecten connector run {decl.name}"
            run_items.append(
                Item(
                    "command",
                    f"Run {decl.name} locally",
                    command if has_fixtures else f"{command} --record",
                )
            )
        elif not has_fixtures:
            fixture_items.append(
                Item(
                    "command",
                    f"Record fixtures for {decl.name}",
                    f"vecten connector run {decl.name} --record",
                )
            )
    return items + run_items + fixture_items


def access_apply_items(project_root: Path, config: VectenConfig, env: str) -> list[Item]:
    facts = config.environments.get(env)
    if facts is None or not facts.deployed_commit:
        return []
    items: list[Item] = []
    for decl in config.connectors:
        if not decl.access or not decl.access.groups or decl.name in facts.connector_access_applied:
            continue
        commands = []
        for group in decl.access.groups:
            commands.append(f"vecten group add {group} --env {env}")
            commands.append(f"vecten grant --schema raw_{decl.name} --group {group} --env {env}")
        items.append(
            Item(
                "command",
                f"Grant {', '.join(decl.access.groups)} access to raw_{decl.name}",
                " && ".join(commands),
            )
        )
    return items
