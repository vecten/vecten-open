"""dlt's verified sources: list them, copy one into the project."""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vecten.cli.injection import load_runner
from vecten_shared.output import ExitCode, Failure


@dataclass(frozen=True)
class SourceFiles:
    requirements: list[str]


class VerifiedSources(Protocol):
    def names(self) -> set[str]: ...
    def materialize(self, name: str, target: Path) -> SourceFiles: ...


# The requirement named exactly `dlt`: vecten pins dlt itself, `dltflow` and the like stay.
_DLT_REQUIREMENT = re.compile(r"dlt(?=$|[\s\[;<>=!~])")


@contextmanager
def _in_directory(path: Path):
    """dlt writes into DLT_PROJECT_DIR when it is set, else into the working directory."""
    previous = Path.cwd()
    project_dir = os.environ.pop("DLT_PROJECT_DIR", None)
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
        if project_dir is not None:
            os.environ["DLT_PROJECT_DIR"] = project_dir


@contextmanager
def _quiet_dlt():
    """dlt's CLI echoes progress and asks to confirm; `--json` stdout must stay one document."""
    from dlt._workspace.cli import echo

    captured = io.StringIO()
    with redirect_stdout(captured), echo.always_choose(True, None):
        yield captured


class DltVerifiedSources:
    @staticmethod
    def parse_requirements(text: str) -> list[str]:
        lines = [line.strip() for line in text.splitlines()]
        return sorted(
            line
            for line in lines
            if line and not line.startswith("#") and not _DLT_REQUIREMENT.match(line)
        )

    def names(self) -> set[str]:
        from dlt._workspace.cli import DEFAULT_VERIFIED_SOURCES_REPO, _init_command

        try:
            with _quiet_dlt():
                listing = _init_command._list_verified_sources(DEFAULT_VERIFIED_SOURCES_REPO)
        except Exception as exc:
            raise Failure(
                ExitCode.TOOL_FAILED, f"could not list dlt verified sources: {exc}"
            ) from None
        return set(listing)

    def materialize(self, name: str, target: Path) -> SourceFiles:
        from dlt._workspace.cli import DEFAULT_VERIFIED_SOURCES_REPO, _init_command

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # dlt init writes into the working directory: <name>/, the pipeline script,
            # requirements.txt and .dlt/.
            with _in_directory(work), _quiet_dlt() as output:
                try:
                    _init_command.init_command(name, "postgres", DEFAULT_VERIFIED_SOURCES_REPO)
                except Exception as exc:
                    raise Failure(ExitCode.TOOL_FAILED, f"dlt init {name} failed: {exc}") from None
            package = work / name
            if not (package / "__init__.py").is_file():
                tail = output.getvalue()[-500:].strip()
                raise Failure(
                    ExitCode.TOOL_FAILED, f"dlt init {name} produced no source package: {tail}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(package, target)
            requirements = work / "requirements.txt"
            text = requirements.read_text(encoding="utf-8") if requirements.is_file() else ""
        return SourceFiles(requirements=self.parse_requirements(text))


def get_verified_sources() -> VerifiedSources:
    return load_runner("VECTEN_VERIFIED_SOURCES", DltVerifiedSources)
