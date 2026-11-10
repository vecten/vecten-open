"""The intake items `next` reports before a project exists, from the answers file only."""

from __future__ import annotations

from pathlib import Path

from vecten.intake.answers import ANSWER_KEYS, read_answers
from vecten.next.items import Item
from vecten.scaffold.params import HAS_WAREHOUSE_YES

NEW_WAREHOUSE_KEYS = ("sources", "warehouse")
EXISTING_WAREHOUSE_KEYS = ("warehouse_url",)
PROJECT_KEYS = ("name", "users")
SOURCES_HINT = ("start empty",)


def _ask(key: str) -> Item:
    spec = ANSWER_KEYS[key]
    allowed = SOURCES_HINT if key == "sources" else spec.choices or ()
    return Item("ask", spec.question, spec.question, answer_keys=(key,), allowed=allowed)


def intake_items(directory: Path) -> list[Item]:
    answers = read_answers(directory)
    if answers.get("has_warehouse") is None:
        return [_ask("has_warehouse")]
    # A hand-edited answers file may hold a YAML boolean; `vecten init` reads it the same way.
    existing = str(answers["has_warehouse"]).lower() in HAS_WAREHOUSE_YES
    branch = EXISTING_WAREHOUSE_KEYS if existing else NEW_WAREHOUSE_KEYS
    # An empty `sources` list is the recorded "start empty" answer, so only absence counts.
    for key in (*branch, *PROJECT_KEYS):
        if answers.get(key) is None:
            return [_ask(key)]
    return [Item("command", "Create the project", "vecten init")]
