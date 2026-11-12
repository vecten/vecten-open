"""`extensions.lock.json`: what each locked extension declares, read without importing it.

`vecten lock` writes it; the CLI, the image build and both runtimes read it, so a project's
CI and hosts know what an extension needs without installing the extension.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RECORD_FILE = "extensions.lock.json"


@dataclass(frozen=True)
class Extension:
    version: str
    extras: tuple[str, ...]
    secrets: tuple[str, ...] = ()
    # The manifest's settings schema, or None when the extension declares no settings.
    settings: dict[str, Any] | None = field(default=None, hash=False)
    # An in-repo extension's project-relative path, its pyproject.toml's sha256, and its build
    # requirements pinned exactly; None and empty for an extension from an index.
    source_path: str | None = None
    metadata_sha256: str | None = None
    build_requires: tuple[str, ...] = ()


def record_text(record: dict[str, Extension]) -> str:
    body = {
        "version": 1,
        "extensions": {
            name: {
                "version": entry.version,
                "extras": sorted(entry.extras),
                "secrets": sorted(entry.secrets),
                "settings": entry.settings,
                **(
                    {
                        "source": {"path": entry.source_path},
                        "metadata_sha256": entry.metadata_sha256,
                        "build_requires": list(entry.build_requires),
                    }
                    if entry.source_path
                    else {}
                ),
            }
            for name, entry in sorted(record.items())
        },
    }
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def parse_record(text: str) -> dict[str, Extension] | None:
    """The record, or None when any part is malformed: a partial record is never trusted."""
    try:
        entries = json.loads(text)["extensions"].items()
        record = {}
        for name, entry in entries:
            version, extras = entry["version"], entry["extras"]
            secrets, settings = entry.get("secrets", []), entry.get("settings")
            source = entry.get("source") or {}
            path, digest = source.get("path"), entry.get("metadata_sha256")
            build = entry.get("build_requires", [])
            if (
                not isinstance(version, str)
                or not isinstance(secrets, list)
                or not all(isinstance(e, str) for e in extras)
                or not all(isinstance(s, str) for s in secrets)
                or not (settings is None or isinstance(settings, dict))
                or not (path is None or (isinstance(path, str) and isinstance(digest, str)))
                or not all(isinstance(b, str) for b in build)
            ):
                return None
            record[name] = Extension(
                version=version,
                extras=tuple(sorted(extras)),
                secrets=tuple(sorted(secrets)),
                settings=settings,
                source_path=path,
                metadata_sha256=digest,
                build_requires=tuple(build),
            )
        return record
    except ValueError, KeyError, TypeError, AttributeError:
        return None


def read_record(root: Path) -> dict[str, Extension] | None:
    path = root / RECORD_FILE
    if not path.is_file():
        return None
    return parse_record(path.read_text(encoding="utf-8"))
