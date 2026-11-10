from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vecten_shared.output import ExitCode, Failure


@dataclass
class CliContext:
    cwd: Path
    json: bool

    def project_root(self) -> Path | None:
        for candidate in (self.cwd, *self.cwd.parents):
            if (candidate / "vecten.toml").is_file():
                return candidate
        return None

    def require_project(self) -> Path:
        root = self.project_root()
        if root is None:
            raise Failure(ExitCode.REFUSED, "not inside a vecten project; run `vecten init` first")
        return root
