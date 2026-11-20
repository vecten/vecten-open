"""Print the project connector dependencies for the generated CI replay environments."""

from pathlib import Path

from packaging.specifiers import SpecifierSet

from vecten import locking
from vecten_shared.config import load_config
from vecten_shared.requirements import requirement
from vecten_shared.versions import normalize_name


def connector_requirements(root: Path) -> list[str]:
    config = load_config(root)
    lock = root / locking.LOCK_FILES["cli"]
    pins = locking.lock_pins(lock.read_text()) if lock.is_file() else {}
    dependencies = set()
    for connector in config.connectors:
        for declaration in connector.dependencies:
            req = requirement(declaration)
            version = pins.get(normalize_name(req.name))
            if version and version in req.specifier:
                req.specifier = SpecifierSet(f"=={version}")
            dependencies.add(str(req))
    return sorted(dependencies)


if __name__ == "__main__":
    for dependency in connector_requirements(Path.cwd()):
        print(dependency)
