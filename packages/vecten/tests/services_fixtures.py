"""A project with one extension, `acme`, whose record declares a service `bot`."""

import tomllib
from pathlib import Path

import tomli_w

from vecten_shared.extension_record import Extension, record_text
from vecten_shared.services import parse_service

BOT = {
    "command": ["python", "-m", "acme.bot"],
    "port": 8000,
    "health": {"path": "/healthz"},
    "routes": {
        "webhook": {"path": "/api/messages", "access": "public"},
        "admin": {"path": "/", "access": "login", "groups": ["default"]},
    },
}


def with_services(root: Path, bindings: dict, declared: dict | None = None, **entry) -> None:
    doc = tomllib.loads((root / "vecten.toml").read_text())
    doc["extensions"] = ["acme"]
    doc["services"] = {"acme": bindings} if bindings else {}
    (root / "vecten.toml").write_text(tomli_w.dumps(doc))
    services = {
        name: parse_service("acme", name, value).service
        for name, value in (declared if declared is not None else {"bot": BOT}).items()
    }
    (root / "extensions.lock.json").write_text(
        record_text({"acme": Extension("1.0", (), services=services, **entry)})
    )
