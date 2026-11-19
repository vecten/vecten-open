"""A project with one extension, `acme`, whose record declares a service `bot`."""

import tomllib
from pathlib import Path

import tomli_w

from vecten_shared.config import load_config
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


OVERVIEW = {
    "title": "Sales overview",
    "description": "",
    "kind": "container",
    "runtime": "streamlit",
    "entry": "acme/app.py",
    "datasets": ["orders"],
    "groups": ["default"],
}


def with_apps(root: Path, bindings: dict, declared: dict | None = None) -> None:
    """`acme` declares the apps in `declared` (the container app `overview` by default)."""
    from vecten_shared.apps import parse_app

    doc = tomllib.loads((root / "vecten.toml").read_text())
    doc["extensions"] = ["acme"]
    doc["apps"] = {"acme": bindings} if bindings else {}
    (root / "vecten.toml").write_text(tomli_w.dumps(doc))
    apps = {
        name: parse_app("acme", name, value).app
        for name, value in (declared if declared is not None else {"overview": OVERVIEW}).items()
    }
    (root / "extensions.lock.json").write_text(
        record_text({"acme": Extension("1.0", (), apps=apps)})
    )


def locked(root):
    """Stamps every lock fresh for the project's `acme==1.0` and its record."""
    from vecten import locking
    from vecten_shared.extension_record import read_record

    (root / "requirements.txt").write_text("acme==1.0\n")
    config, record = load_config(root), read_record(root)
    for component in locking.components(config):
        roots = locking.roots(config, locking.read_pins(root), record, component)
        digest = locking.inputs_hash(component, [*roots, *locking.metadata_markers(record)])
        body = "".join(f"{r}\n" for r in roots)
        (root / locking.LOCK_FILES[component]).write_text(
            locking.stamp_line(component, digest) + "\n" + body
        )
    return root
