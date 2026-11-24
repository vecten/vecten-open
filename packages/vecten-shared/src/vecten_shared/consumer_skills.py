"""Package files an installed extension explicitly offers to a data user's assistant."""

import io
import stat
import zipfile
from pathlib import Path

from vecten_shared.extensions import load_group
from vecten_shared.names import SKILL_NAME
from vecten_shared.output import ExitCode, Failure

GROUP = "vecten.consumer_skills"


def package_skill(name: str, declaration: object) -> bytes:
    if not SKILL_NAME.fullmatch(name):
        raise Failure(ExitCode.CONFIG, f"invalid consumer skill name {name!r}")
    if not isinstance(declaration, dict) or not isinstance(
        declaration.get("directory"), (str, Path)
    ):
        raise Failure(ExitCode.CONFIG, f"consumer skill {name} needs a directory")
    if unknown := declaration.keys() - {"directory", "files"}:
        raise Failure(ExitCode.CONFIG, f"consumer skill {name}: unknown fields {sorted(unknown)}")
    directory = Path(declaration["directory"])
    files = declaration.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise Failure(ExitCode.CONFIG, f"consumer skill {name}: files must be a list of paths")
    if directory.is_symlink() or not directory.is_dir():
        raise Failure(ExitCode.CONFIG, f"consumer skill {name}: expected a skill directory")
    directory = directory.resolve()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as out:
        seen = set()
        for member in sorted(["SKILL.md", *files]):
            parts = member.split("/")
            if any(
                not p or p.startswith(".") or "\\" in p or ":" in p or not p.isprintable()
                for p in parts
            ):
                raise Failure(
                    ExitCode.CONFIG, f"consumer skill {name}: invalid file path {member!r}"
                )
            path = directory.joinpath(*parts)
            if path.resolve() != path or not path.is_file():
                raise Failure(
                    ExitCode.CONFIG,
                    f"consumer skill {name}: {member} must be a regular file in its directory",
                )
            if member.casefold() in seen:
                raise Failure(ExitCode.CONFIG, f"consumer skill {name}: duplicate file {member}")
            seen.add(member.casefold())
            entry = zipfile.ZipInfo(f"{name}/{member}")
            entry.compress_type = zipfile.ZIP_DEFLATED
            permissions = 0o755 if path.stat().st_mode & 0o111 else 0o644
            entry.external_attr = (stat.S_IFREG | permissions) << 16
            out.writestr(entry, path.read_bytes())
    return archive.getvalue()


def skill_packages(config, names):
    declarations = load_group(config, GROUP)
    result = {}
    for name in names:
        if name not in declarations:
            raise Failure(ExitCode.CONFIG, f"no listed extension declares consumer skill {name}")
        result[name] = package_skill(name, declarations[name])
    return result
