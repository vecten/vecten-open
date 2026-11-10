"""Connectors that installed extensions offer through the `vecten.connectors` entry point group."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path

from vecten_shared.config import SOURCE_KINDS, VectenConfig
from vecten_shared.extensions import selected_entry_points
from vecten_shared.names import SECRET_NAME, SECRET_RULE
from vecten_shared.output import ExitCode, Failure

GROUP = "vecten.connectors"


@dataclass(frozen=True)
class ExtensionConnector:
    name: str
    package: str
    version: str
    kind: str
    secrets: list[str]
    dependencies: list[str]
    write: Callable[[Path], None]


def entry_points_for(group: str):
    return list(entry_points(group=group))


def _load_spec(ep, package: str) -> dict:
    where = f"extension {package}, entry point {ep.name}"
    try:
        factory = ep.load()
        spec = factory() if callable(factory) else factory
    except Exception as exc:
        raise Failure(
            ExitCode.CONFIG, f"{where} failed to load: {type(exc).__name__}: {exc}"
        ) from None
    if not isinstance(spec, dict):
        raise Failure(ExitCode.CONFIG, f"{where} must provide a dict, got {type(spec).__name__}")
    if not callable(spec.get("write")):
        raise Failure(ExitCode.CONFIG, f"{where} has no callable write")
    return spec


def _string_list(spec: dict, key: str, package: str, connector: str) -> list[str]:
    # A bare string would otherwise split into one entry per character.
    value = spec.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise Failure(
            ExitCode.CONFIG,
            f"extension {package} declares connector {connector} with {key} that is not a "
            f"list of strings: {value!r}",
        )
    return list(value)


def extension_connectors(config: VectenConfig) -> dict[str, ExtensionConnector]:
    found: dict[str, ExtensionConnector] = {}
    for ep in selected_entry_points(config, GROUP, entry_points_for(GROUP)):
        dist = getattr(ep, "dist", None)
        package = dist.name if dist is not None else ""
        spec = _load_spec(ep, package)
        kind = spec.get("kind", "custom")
        if kind not in SOURCE_KINDS:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {package} declares connector {ep.name} with unknown kind {kind!r}",
            )
        secrets = _string_list(spec, "secrets", package, ep.name)
        dependencies = _string_list(spec, "dependencies", package, ep.name)
        bad = [s for s in secrets if not SECRET_NAME.fullmatch(s)]
        if bad:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {package} declares connector {ep.name} with invalid secret names "
                f"{bad!r}; a secret name is {SECRET_RULE}",
            )
        found[ep.name] = ExtensionConnector(
            name=ep.name,
            package=package,
            version=getattr(dist, "version", "") or "",
            kind=kind,
            secrets=secrets,
            dependencies=dependencies,
            write=spec["write"],
        )
    return found
