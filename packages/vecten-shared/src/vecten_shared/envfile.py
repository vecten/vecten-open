"""One reader for a project's .env: a missing file holds no secrets, an unreadable one stops."""

from __future__ import annotations

import io
from pathlib import Path

from dotenv import dotenv_values

from vecten_shared.output import ExitCode, Failure


def read_env_text(path: Path) -> str | None:
    # pathlib's is_file() answers False on any OSError, so a file a sandbox denies would read
    # as missing; only FileNotFoundError means there is no file.
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Failure(ExitCode.PREREQUISITE, f"{path} cannot be read: {exc.strerror}") from exc


def read_env_values(path: Path) -> dict[str, str | None]:
    text = read_env_text(path)
    if text is None:
        return {}
    return dotenv_values(stream=io.StringIO(text), interpolate=False)
