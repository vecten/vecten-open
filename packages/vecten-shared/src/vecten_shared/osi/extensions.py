"""The Vecten extension: COMMON vendor, JSON string data, under the `vecten` key.

The pinned 0.1.1 schema and dbt's parser close the vendor enum, so a vendor named `vecten`
would fail `dbt parse` on every project; the COMMON slot is the one vendor-neutral entry.
"""

from __future__ import annotations

import json

VECTEN_VENDOR = "COMMON"
KEY = "vecten"


def vecten_extension(data: dict) -> dict:
    return {"vendor_name": VECTEN_VENDOR, "data": json.dumps({KEY: data}, sort_keys=True)}


def read_vecten_extension(obj: dict) -> dict | None:
    found: list[dict] = []
    for entry in obj.get("custom_extensions") or []:
        if not isinstance(entry, dict) or entry.get("vendor_name") != VECTEN_VENDOR:
            continue
        try:
            payload = json.loads(entry.get("data", ""))
        except TypeError, ValueError:
            continue
        if isinstance(payload, dict) and KEY in payload:
            if not isinstance(payload[KEY], dict):
                raise ValueError("the Vecten extension data must be a JSON object")
            found.append(payload[KEY])
    if len(found) > 1:
        raise ValueError("at most one Vecten extension per element")
    return found[0] if found else None
