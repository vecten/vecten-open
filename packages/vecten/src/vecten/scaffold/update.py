"""Upgrade a project's generated files to the installed lockstep release."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml
from packaging.version import InvalidVersion, Version

from vecten.scaffold.baseline import ANSWERS, BASELINE, baseline_text, checked_path, read_baseline
from vecten.scaffold.merge import merge_snapshots
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure, Result
from vecten_shared.records import write_text_atomic
from vecten_shared.versions import VERSIONS


def update_project(root: Path) -> Result:
    root = root.resolve()
    for name in (ANSWERS, BASELINE, "vecten.toml"):
        checked_path(root, name)
    load_config(root)
    try:
        answers = yaml.safe_load((root / ANSWERS).read_text())
        if not isinstance(answers, dict):
            raise ValueError("expected a mapping")
        old_version = answers["vecten_version"]
        if Version(old_version) > Version(VERSIONS.vecten):
            raise Failure(ExitCode.REFUSED, "update refuses to downgrade the scaffold")
        params = ScaffoldParams.model_validate(
            {
                k: None if v == "" else v
                for k, v in answers.items()
                if k in ScaffoldParams.model_fields
            }
        )
    except (OSError, ValueError, KeyError, TypeError, InvalidVersion, yaml.YAMLError) as exc:
        raise Failure(ExitCode.CONFIG, f"{ANSWERS}: {exc}") from None
    old = read_baseline(root)
    if old is not None and old["version"] != old_version:
        raise Failure(ExitCode.CONFIG, "scaffold baseline and saved answers versions disagree")
    if old is None and old_version != VERSIONS.vecten:
        raise Failure(
            ExitCode.REFUSED,
            "older prerelease project has no pristine scaffold baseline; "
            "initialize upgrade metadata with its original CLI before upgrading",
        )
    with tempfile.TemporaryDirectory(prefix="vecten-update-") as tmp:
        work = Path(tmp).resolve()
        rendered = work / "rendered"
        render_project(rendered, params)
        new = json.loads((rendered / BASELINE).read_text())
        previous = old["files"] if old else new["files"]
        names = previous.keys() | new["files"].keys()
        paths = {name: checked_path(root, name) for name in names}
        current = {name: path.read_text() for name, path in paths.items() if path.is_file()}
        merged, conflicts = merge_snapshots(work, previous, new["files"], current)
        if conflicts:
            raise Failure(
                ExitCode.REFUSED,
                "update conflicts; no project files changed: " + ", ".join(conflicts),
                details={"conflicts": conflicts},
            )
        # Configuration must still parse after the merge, before any project file is touched.
        load_config(work / "merged")
        merged[BASELINE] = baseline_text(new["version"], new["files"])
        paths[BASELINE] = root / BASELINE
        current[BASELINE] = (root / BASELINE).read_text() if old else None
        changed = sorted(name for name in paths if current.get(name) != merged.get(name))
        # Back up only participating generated files; never read unrelated files or secrets.
        originals = {
            name: paths[name].read_bytes() if paths[name].is_file() else None for name in changed
        }
        try:
            for name in changed:
                path = checked_path(root, name)
                if name in merged:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(path, merged[name])
                else:
                    path.unlink(missing_ok=True)
        except BaseException:
            for name, content in originals.items():
                if content is None:
                    paths[name].unlink(missing_ok=True)
                else:
                    paths[name].write_bytes(content)
            raise
    return Result(
        data={
            "from_version": old_version,
            "to_version": VERSIONS.vecten,
            "changed": changed,
            "conflicts": [],
            "adopted_baseline": old is None,
        },
        message=f"updated {old_version} → {VERSIONS.vecten}; "
        f"{len(changed)} files changed"
        + ("; initialized pristine upgrade metadata" if old is None else ""),
    )
