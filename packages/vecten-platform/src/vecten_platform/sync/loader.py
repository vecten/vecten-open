"""Imports connectors/<name>/connector.py as a package module so its relative imports work.

Every call imports the connector fresh, because connector files change between runs and one
process may load connectors from several projects. The project root is appended to `sys.path`,
never prepended, so its directories (dbt/, osi/, docs/) never shadow installed packages.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from vecten_shared.output import ExitCode, Failure


def _located_in(module: ModuleType, directory: Path) -> bool:
    locations = list(getattr(module, "__path__", None) or [])
    return any(Path(location).resolve() == directory for location in locations)


def _forget(prefix: str) -> None:
    for key in [k for k in sys.modules if k == prefix or k.startswith(f"{prefix}.")]:
        del sys.modules[key]


def _bind_package(connectors_dir: Path) -> None:
    # Bound by location, not by sys.path search: an earlier project's root may sit before this
    # one on the path and hold a package with the same name.
    package = connectors_dir.name
    current = sys.modules.get(package)
    if current is not None and _located_in(current, connectors_dir):
        return
    _forget(package)
    init = connectors_dir / "__init__.py"
    if init.is_file():
        spec = importlib.util.spec_from_file_location(
            package, init, submodule_search_locations=[str(connectors_dir)]
        )
    else:
        spec = importlib.machinery.ModuleSpec(package, None, is_package=True)
        spec.submodule_search_locations = [str(connectors_dir)]
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    if spec.loader is not None:
        spec.loader.exec_module(module)


def load_connector(connectors_dir: Path, name: str) -> ModuleType:
    package_dir = connectors_dir / name
    if not (package_dir / "connector.py").is_file():
        raise Failure(
            ExitCode.REFUSED,
            f"{package_dir} does not exist; run `vecten connector add {name}`",
        )
    resolved = connectors_dir.resolve()
    parent = str(resolved.parent)
    if parent not in sys.path:
        sys.path.append(parent)
    module_name = f"{resolved.name}.{name}.connector"
    try:
        _bind_package(resolved)
        _forget(f"{resolved.name}.{name}")
        importlib.invalidate_caches()
        module = importlib.import_module(module_name)
    except Failure:
        raise
    except Exception as exc:
        _forget(resolved.name)
        raise Failure(
            ExitCode.CONFIG, f"{package_dir / 'connector.py'}: {type(exc).__name__}: {exc}"
        ) from None
    if not callable(getattr(module, "source", None)):
        raise Failure(
            ExitCode.CONFIG,
            f"{package_dir / 'connector.py'} must define source() returning the dlt source",
        )
    return module
