"""A project with one extension whose manifest declares a typed setting."""

import tomllib
from pathlib import Path

import tomli_w

from vecten_shared.extension_record import Extension, record_text

SCHEMA = {"properties": {"daily_cap": {"type": "integer", "default": 1000}}}


def with_settings(root: Path, settings: dict) -> None:
    doc = tomllib.loads((root / "vecten.toml").read_text())
    doc["extensions"] = ["acme"]
    doc["settings"] = {"acme": settings}
    (root / "vecten.toml").write_text(tomli_w.dumps(doc))
    (root / "extensions.lock.json").write_text(
        record_text({"acme": Extension("1.0", (), settings=SCHEMA)})
    )
