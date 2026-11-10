"""The item shape `next` reports: an agent acts on it without interpreting it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ItemKind = Literal["command", "skill", "ask"]


@dataclass(frozen=True)
class Item:
    kind: ItemKind
    title: str
    resolution: str
    answer_keys: tuple[str, ...] = ()
    allowed: tuple[str, ...] = ()
    show_first: tuple[str, ...] = ()

    def to_json(self) -> dict:
        doc = {"kind": self.kind, "title": self.title, "resolution": self.resolution}
        if self.kind == "ask":
            doc |= {
                "answer_keys": list(self.answer_keys),
                "allowed": list(self.allowed),
                "show_first": list(self.show_first),
            }
        return doc
