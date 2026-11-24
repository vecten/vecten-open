"""Package files an installed extension explicitly offers to a data user's assistant."""

import io
import stat
import zipfile
from pathlib import Path

from vecten_shared.extensions import selected_entry_points
from vecten_shared.names import SKILL_NAME
from vecten_shared.output import ExitCode, Failure
from vecten_shared.requirements import requirement
from vecten_shared.versions import normalize_name

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


def installed_skills(config, names):
    points = {p.name: p for p in selected_entry_points(config, GROUP)}
    for name in names:
        if name not in points:
            raise Failure(ExitCode.CONFIG, f"no listed extension declares consumer skill {name}")
        point = points[name]
        try:
            declaration = point.load()
        except Exception as exc:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {point.dist.name}: cannot load consumer skill {name} "
                f"({type(exc).__name__})",
            ) from exc
        yield name, normalize_name(point.dist.name), package_skill(name, declaration)


def skill_packages(config, names):
    return {name: archive for name, _, archive in installed_skills(config, names)}


def locked_skill_owners(config, record):
    listed = {normalize_name(requirement(line).name) for line in config.extensions}
    owners = {}
    for owner in listed.intersection(record):
        for name in record[owner].consumer_skills:
            if name in owners:
                raise Failure(
                    ExitCode.CONFIG,
                    f"consumer skill {name} has multiple owners in extensions.lock.json",
                )
            owners[name] = owner
    for name in config.consumer_skills:
        if name not in owners:
            raise Failure(
                ExitCode.CONFIG,
                f"consumer skill {name} is missing from extensions.lock.json; run `vecten lock`",
            )
    return {name: owners[name] for name in config.consumer_skills}


if __name__ == "__main__":
    import sys

    from vecten_shared.config import VectenConfig

    config = VectenConfig.model_validate_json(Path(sys.argv[1]).read_bytes())
    try:
        skill_packages(config, config.consumer_skills)
    except Failure as error:
        sys.exit(error.message)
