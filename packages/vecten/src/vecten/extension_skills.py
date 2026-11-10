"""Machine-local links expose installed skills without vendoring extension files."""

import json
import os
import re
from pathlib import Path

from vecten.knowhow import skill_names
from vecten_shared.config import VectenConfig
from vecten_shared.extensions import extension_paths
from vecten_shared.output import ExitCode, Failure

RECORD = Path(".vecten/extension-skills.json")


def _check_parents(root: Path, target: Path) -> None:
    for parent in (target.parent, *target.parent.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise Failure(ExitCode.REFUSED, "extension skill parent is a symbolic link")


def skill_links(root: Path, config: VectenConfig) -> dict[Path, Path]:
    links = {}
    for name, directory in extension_paths(config, "vecten.skills").items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in skill_names():
            raise Failure(ExitCode.CONFIG, f"invalid or reserved extension skill name {name}")
        if not (directory / "SKILL.md").is_file():
            raise Failure(ExitCode.CONFIG, f"extension skill {name} has no SKILL.md")
        for target in (root / "skills" / name, root / "plugins" / "vecten" / "skills" / name):
            _check_parents(root, target)
            if target.exists() and not target.is_symlink():
                raise Failure(ExitCode.REFUSED, f"refusing to overwrite project skill {name}")
            links[target] = directory
    return links


def skill_artifacts(root: Path, config: VectenConfig) -> dict[Path, str | Path | None]:
    links = skill_links(root, config)
    record = root / RECORD
    _check_parents(root, record)
    old = {}
    if record.exists():
        try:
            old = json.loads(record.read_text())
            if not isinstance(old, dict) or not all(
                isinstance(v, str)
                and re.fullmatch(r"(?:skills|plugins/vecten/skills)/[a-z][a-z0-9_-]*", v)
                and isinstance(destination, str)
                and Path(destination).is_absolute()
                for v, destination in old.items()
            ):
                raise ValueError()
        except ValueError:
            raise Failure(ExitCode.CONFIG, "invalid extension skill link record") from None
    files = dict(links)
    for target in set(links) | {root / relative for relative in old}:
        _check_parents(root, target)
        if target.is_symlink():
            previous = old.get(str(target.relative_to(root)))
            if previous != os.readlink(target):
                raise Failure(
                    ExitCode.REFUSED, f"refusing to overwrite project skill {target.name}"
                )
            if target not in links:
                files[target] = None
        elif target.exists():
            raise Failure(ExitCode.REFUSED, f"refusing to overwrite project skill {target.name}")
    if not links and not old:
        return {}
    files[record] = (
        json.dumps({str(p.relative_to(root)): str(destination) for p, destination in links.items()})
        + "\n"
    )
    ignore = root / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    missing = [
        "/" + str(p.relative_to(root))
        for p in links
        if "/" + str(p.relative_to(root)) not in text.splitlines()
    ]
    if missing:
        files[ignore] = text.rstrip("\n") + "\n" + "\n".join(missing) + "\n"
    from vecten.knowhow.render import plugin_manifest_text

    manifest = root / "plugins/vecten/.claude-plugin/plugin.json"
    _check_parents(root, manifest)
    if not manifest.exists():
        files[manifest] = plugin_manifest_text()
    return files


def link_extension_skills(root: Path, config: VectenConfig) -> None:
    from vecten.extensions import write_project_files

    write_project_files(skill_artifacts(root, config))
