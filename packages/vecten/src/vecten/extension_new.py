"""`vecten extension new`: copy a template into extensions/<name>/ once, then register it.

The copy belongs to the project from then on; nothing records where it came from, and a git
template is read once with the person's own git setup.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from vecten.extensions import add_extension
from vecten.in_repo import EXTENSIONS_DIR
from vecten_shared.output import ExitCode, Failure
from vecten_shared.versions import VERSIONS

SKELETON = Path(__file__).parent / "extension_skeleton"
TEMPLATE_FILE = "vecten-template.toml"
SKIPPED = frozenset({".git", "__pycache__", ".venv", "dist", "build", TEMPLATE_FILE})
# A normalised distribution name: what the directory and every record use.
NAME = re.compile(r"[a-z0-9]([a-z0-9]|-(?=[a-z0-9]))*")


def _placeholders(name: str) -> dict[str, str]:
    return {
        "__vecten_name__": name,
        "__vecten_package__": name.replace("-", "_"),
        "__vecten_title__": " ".join(word.capitalize() for word in name.split("-")),
        "__vecten_version__": VERSIONS.vecten,
    }


def _substitute(text: str, values: dict[str, str]) -> str:
    for token, value in values.items():
        text = text.replace(token, value)
    return text


def _clone(url: str, ref: str, destination: Path) -> None:
    """Fetch `ref` of `url` with the person's own git configuration, credentials and prompts."""
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref):
        commands = [
            ["git", "init", "-q", str(destination)],
            ["git", "-C", str(destination), "fetch", "-q", "--depth", "1", url, ref],
            ["git", "-C", str(destination), "checkout", "-q", "FETCH_HEAD"],
        ]
    else:
        commands = [["git", "clone", "-q", "--depth", "1", "--branch", ref, url, str(destination)]]
    for command in commands:
        if subprocess.run(command).returncode:
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"could not read the template at {ref}; check that your git setup can read "
                "the repository and that the ref exists",
            )


def _git_source(spec: str, temp: Path) -> Path:
    """`git+https://<host>/<repo>@<ref>[#subdirectory=<dir>]`, read into `temp`."""
    location, _, fragment = spec[len("git+") :].partition("#")
    parts = urlsplit(location)
    if parts.scheme != "https" or "@" in parts.netloc or parts.query:
        raise Failure(
            ExitCode.USAGE,
            "a git template is git+https://<host>/<repository>@<ref>, with no credentials in "
            "the URL; your git setup provides them",
        )
    repository, _, ref = location.rpartition("@")
    if not ref or not repository or not urlsplit(repository).path.strip("/"):
        raise Failure(ExitCode.USAGE, "a git template names its ref: git+https://…/repo@<tag>")
    subdirectory = ""
    if fragment:
        key, _, subdirectory = fragment.partition("=")
        relative = PurePosixPath(subdirectory)
        if key != "subdirectory" or relative.is_absolute() or ".." in relative.parts:
            raise Failure(ExitCode.USAGE, "the only fragment is #subdirectory=<path inside>")
    _clone(repository, ref, temp)
    return temp / subdirectory if subdirectory else temp


def _check_template(source: Path) -> None:
    if not source.is_dir():
        raise Failure(ExitCode.USAGE, f"template {source} is not a directory")
    declared = source / TEMPLATE_FILE
    if not declared.is_file():
        return
    try:
        requires = tomllib.loads(declared.read_text(encoding="utf-8")).get("requires_vecten")
        if requires and VERSIONS.vecten not in SpecifierSet(requires):
            raise Failure(
                ExitCode.REFUSED,
                f"the template requires Vecten Open {requires}; this is {VERSIONS.vecten}",
            )
    except (tomllib.TOMLDecodeError, InvalidSpecifier, UnicodeDecodeError) as exc:
        raise Failure(ExitCode.CONFIG, f"{TEMPLATE_FILE}: {exc}") from None


def _copy(source: Path, target: Path, values: dict[str, str]) -> None:
    for current, directories, files in os.walk(source):
        here = Path(current)
        directories[:] = [d for d in directories if d not in SKIPPED]
        for name in [*directories, *files]:
            if (here / name).is_symlink():
                raise Failure(
                    ExitCode.REFUSED,
                    f"the template holds a symbolic link at {(here / name).relative_to(source)}",
                )
        relative = here.relative_to(source)
        destination = target / _substitute(str(relative), values)
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name in SKIPPED:
                continue
            path = destination / _substitute(name, values)
            data = (here / name).read_bytes()
            try:
                path.write_text(_substitute(data.decode("utf-8"), values), encoding="utf-8")
            except UnicodeDecodeError:
                path.write_bytes(data)
            mode = (here / name).stat().st_mode
            if mode & stat.S_IXUSR:
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def new_extension(root: Path, name: str, *, template: str | None = None, add: bool = True) -> dict:
    if not NAME.fullmatch(name):
        raise Failure(
            ExitCode.USAGE,
            f"{name!r} is not a normalised distribution name: lowercase letters, digits and "
            "single hyphens, such as acme-portal",
        )
    target = root / EXTENSIONS_DIR / name
    if target.exists():
        raise Failure(ExitCode.REFUSED, f"{EXTENSIONS_DIR}/{name} already exists; edit it instead")
    (root / EXTENSIONS_DIR).mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vecten-template-") as temp:
        if template is None:
            source = SKELETON
        elif template.startswith("git+"):
            source = _git_source(template, Path(temp) / "checkout")
        else:
            source = Path(template).expanduser()
        _check_template(source)
        # The copy lands beside its target and moves in whole, so a refusal leaves nothing.
        with tempfile.TemporaryDirectory(dir=root / EXTENSIONS_DIR) as staging:
            staged = Path(staging) / name
            _copy(source, staged, _placeholders(name))
            shutil.move(str(staged), str(target))
    path = f"{EXTENSIONS_DIR}/{name}"
    result = {"name": name, "path": path, "registered": False}
    if add:
        result |= {"registered": True, **add_extension(root, f"./{path}")}
    return result
