"""Apache Ossie documents: the pinned schema, the merge, the generator and the validator."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

OSI_VERSION = "0.1.1"


def schema_path() -> Path:
    return Path(
        str(resources.files("vecten_shared.osi").joinpath(f"schema/osi-schema-{OSI_VERSION}.json"))
    )


def load_schema() -> dict:
    return json.loads(schema_path().read_text(encoding="utf-8"))
