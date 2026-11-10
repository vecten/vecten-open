"""Pristine generated text used as the common ancestor of a project upgrade."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path, PurePosixPath

from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic

BASELINE = ".copier-baseline.json"
ANSWERS = ".copier-answers.yml"


def checked_path(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if (
        not parts
        or PurePosixPath(name).is_absolute()
        or "\\" in name
        or any(p in {".", "..", ".git", ".env", ".vecten"} for p in parts)
        or any(p.startswith(".env.") for p in parts)
        or str(PurePosixPath(name)) != name
    ):
        raise Failure(ExitCode.CONFIG, f"invalid scaffold metadata path: {name!r}")
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise Failure(ExitCode.REFUSED, f"update refuses symlink: {name}")
    if path.exists() and not path.is_file():
        raise Failure(ExitCode.REFUSED, f"update expects a file: {name}")
    return path


def baseline_text(version: str, files: dict[str, str]) -> str:
    return (
        json.dumps({"format": 1, "version": version, "files": files}, indent=2, sort_keys=True)
        + "\n"
    )


def read_baseline(root: Path) -> dict | None:
    path = checked_path(root, BASELINE)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if (
            not isinstance(data, dict)
            or data.get("format") != 1
            or not isinstance(data.get("version"), str)
            or not isinstance(data.get("files"), dict)
            or not {ANSWERS, "vecten.toml"} <= data["files"].keys()
        ):
            raise ValueError("expected format 1, version and pristine files")
        for name, text in data["files"].items():
            if not isinstance(text, str) or name == BASELINE:
                raise ValueError("expected text files without recursive baseline")
            checked_path(root, name)
        VectenConfig.model_validate(tomllib.loads(data["files"]["vecten.toml"]))
        return data
    except (ValueError, UnicodeError) as exc:
        raise Failure(ExitCode.CONFIG, f"{BASELINE}: {exc}") from None


def write_baseline(root: Path, version: str, files: dict[str, str]) -> None:
    write_text_atomic(root / BASELINE, baseline_text(version, files))
