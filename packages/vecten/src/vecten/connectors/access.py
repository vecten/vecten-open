"""The access answer: which groups may read a connector's raw schema."""

from __future__ import annotations

from pathlib import Path

from vecten_shared.config import AccessDecl, load_config, save_config
from vecten_shared.names import check_group_name
from vecten_shared.output import ExitCode, Failure

ACCESS_PREFIX = "access."


def is_access_key(key: str) -> bool:
    return key.startswith(ACCESS_PREFIX) and len(key) > len(ACCESS_PREFIX)


def parse_groups(raw: str) -> list[str]:
    groups = sorted({g.strip() for g in raw.split(",") if g.strip()})
    if not groups:
        raise Failure(ExitCode.USAGE, "name at least one group")
    for group in groups:
        check_group_name(group)
    return groups


def set_access(project_root: Path, connector: str, raw: str) -> list[str]:
    config = load_config(project_root)
    decl = config.connector(connector)
    if decl is None:
        known = ", ".join(c.name for c in config.connectors) or "none"
        raise Failure(
            ExitCode.USAGE, f"no connector named {connector!r} in vecten.toml; declared: {known}"
        )
    groups = parse_groups(raw)
    decl.access = AccessDecl(groups=groups)
    save_config(project_root, config)
    return groups
