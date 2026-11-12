"""Distribution-scoped extension discovery shared by the CLI and both runtimes."""

from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure
from vecten_shared.requirements import requirement
from vecten_shared.versions import normalize_name

GROUPS = (
    "vecten.connectors",
    "vecten.dbt_packages",
    "vecten.dags",
    "vecten.osi",
    "vecten.mcp.tools",
    "vecten.mcp.auth",
    "vecten.mcp.provisioning",
    "vecten.mcp.audit_sinks",
    "vecten.skills",
)
RESOURCE_GROUPS = frozenset({"vecten.dbt_packages", "vecten.dags", "vecten.osi", "vecten.skills"})


def selected_entry_points(config: VectenConfig, group: str, candidates=None) -> list:
    if group not in GROUPS:
        raise ValueError(f"unknown extension group {group}")
    wanted = {normalize_name(r.name): r for r in map(requirement, config.extensions)}
    if group == "vecten.mcp.tools":
        wanted.setdefault("vecten-mcp", Requirement("vecten-mcp"))
    points = metadata.entry_points(group=group) if candidates is None else candidates
    selected = {}
    for point in points:
        dist = getattr(point, "dist", None)
        if dist is None or normalize_name(dist.name) not in wanted:
            continue
        req = wanted[normalize_name(dist.name)]
        version = getattr(dist, "version", None)
        if req.specifier and (version is None or version not in req.specifier):
            raise Failure(
                ExitCode.CONFIG,
                f"extension {req.name} installed version does not match {req.specifier}",
            )
        if point.name in selected:
            raise Failure(ExitCode.CONFIG, f"duplicate {group} entry point {point.name}")
        selected[point.name] = point
    return [selected[name] for name in sorted(selected)]


def load_group(config: VectenConfig, group: str) -> dict[str, object]:
    found = {}
    for point in selected_entry_points(config, group):
        try:
            found[point.name] = point.load()
        except Exception as exc:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {point.dist.name}: cannot load {group} entry point {point.name} "
                f"({type(exc).__name__})",
            ) from exc
    return found


def extension_paths(config: VectenConfig, group: str) -> dict[str, Path]:
    if group not in RESOURCE_GROUPS:
        raise ValueError(f"{group} is not a resource group")
    paths = {}
    for name, value in load_group(config, group).items():
        if not isinstance(value, (str, Path)) or not Path(value).is_dir():
            raise Failure(
                ExitCode.CONFIG, f"{group} entry point {name} must name an installed directory"
            )
        paths[name] = Path(value).resolve()
    return paths
