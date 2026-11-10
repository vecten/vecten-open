"""Run records: what the CLI did on this machine, read back by `next`."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vecten_shared.output import ExitCode, Failure

RECORDS_DIR = Path(".vecten") / "records"


def _target_mode(path: Path) -> int:
    # mkstemp creates 0600; the replaced file keeps its own mode, a new one gets what open() would.
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def write_text_atomic(path: Path, text: str, mode: int | None = None) -> None:
    # The temp file must live in the target's directory: os.replace is atomic only
    # within one filesystem, so a reader sees the old file or the new one, never a torn write.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, _target_mode(path) if mode is None else mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_record(project_root: Path, name: str, payload: dict[str, Any]) -> Path:
    path = project_root / RECORDS_DIR / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {**payload, "recorded_at": datetime.now(UTC).isoformat(timespec="seconds")}
    write_text_atomic(path, json.dumps(body, indent=2, sort_keys=True) + "\n")
    return path


def remove_record(project_root: Path, name: str) -> None:
    (project_root / RECORDS_DIR / f"{name}.json").unlink(missing_ok=True)


def read_record(project_root: Path, name: str) -> dict[str, Any] | None:
    path = project_root / RECORDS_DIR / f"{name}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Failure(ExitCode.CONFIG, f"{path}: corrupt run record: {exc}") from None
