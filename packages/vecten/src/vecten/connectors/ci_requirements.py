"""Prepare the dependency and source constraints for connector input replay in CI."""

import sys
from pathlib import Path

from packaging.specifiers import SpecifierSet

from vecten import in_repo, locking
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.requirements import requirement
from vecten_shared.versions import normalize_name


def connector_environment(root: Path) -> tuple[list[str], list[str]]:
    config = load_config(root)
    declarations = {
        dep
        for connector in config.connectors
        if any((root / "connectors" / connector.name / "fixtures" / "inputs").glob("*.json"))
        for dep in connector.dependencies
    }
    if not declarations:
        return [], []
    if config.packages.private_index:
        raise Failure(
            ExitCode.PREREQUISITE,
            "connector input replay needs the project's private package index; "
            "use output fixtures in credential-free CI or run input replay locally",
        )
    record = locking.read_record(root)
    if record is None and (root / locking.RECORD_FILE).exists():
        raise Failure(ExitCode.CONFIG, "invalid extension sources; run vecten lock")
    record = record or {}
    registered = {normalize_name(requirement(r).name) for r in config.extensions}
    paths = {entry.source_path for entry in record.values() if entry.source_path}
    if (
        set(record) != registered
        or paths != locking.listed_paths(root)
        or locking.git_problems(root, config)
    ):
        raise Failure(ExitCode.CONFIG, "extension sources are missing or stale; run vecten lock")
    constraints = []
    git_names = set()
    for name, entry in sorted(record.items()):
        if entry.git is not None:
            git_names.add(name)
            # No version satisfies this constraint, including through a transitive dependency.
            constraints.append(f"{name}<0,>=0")
        elif entry.source_path is not None:
            local = in_repo.check(root, entry.source_path)
            if local.metadata_sha256 != entry.metadata_sha256:
                raise Failure(ExitCode.CONFIG, f"{name} metadata has changed; run vecten lock")
            constraints.append(f"{name} @ {(root / local.path).resolve().as_uri()}")

    lock = root / locking.LOCK_FILES["cli"]
    pins = locking.lock_pins(lock.read_text()) if lock.is_file() else {}
    dependencies = set()
    for declaration in declarations:
        req = requirement(declaration)
        name = normalize_name(req.name)
        if name in git_names:
            raise Failure(
                ExitCode.PREREQUISITE,
                f"{name} is a registered Git extension unavailable in credential-free CI; "
                "run input replay locally after vecten extension sync",
            )
        version = pins.get(name)
        if version and version in req.specifier:
            req.specifier = SpecifierSet(f"=={version}")
        dependencies.add(str(req))
    return sorted(dependencies), constraints


if __name__ == "__main__":
    try:
        requirements, sources = connector_environment(Path.cwd())
    except Failure as exc:
        sys.exit(exc.message)
    output = Path(sys.argv[1])
    (output / "connector-requirements.txt").write_text("".join(f"{r}\n" for r in requirements))
    (output / "connector-sources.txt").write_text("".join(f"{s}\n" for s in sources))
