from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    document: str | None = None
    path: str = ""
    severity: Literal["error", "warning"] = "error"

    def render(self) -> str:
        where = f"{self.document}: " if self.document else ""
        at = f" at {self.path}" if self.path else ""
        return f"{where}{self.code}{at}: {self.message}"


class ModelError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("\n".join(i.render() for i in issues))
