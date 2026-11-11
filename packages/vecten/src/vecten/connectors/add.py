"""`connector add`: stage everything, check everything, then move it into the project."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from vecten.connectors.extensions import ExtensionConnector, ExtensionGroup
from vecten.connectors.inspect_source import secret_params, source_function_name
from vecten.connectors.names import check_connector_name, secret_name
from vecten.connectors.render import (
    GENERIC_SECRET_SUFFIX,
    render_connector_files,
    render_dag_file,
    render_test_file,
    scaffold_hash,
)
from vecten.connectors.sources_yml import ensure_source, load_sources
from vecten.connectors.verified import VerifiedSources
from vecten_shared.config import (
    GENERIC_KINDS,
    ConnectorDecl,
    VectenConfig,
    load_config,
    save_config,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic
from vecten_shared.versions import normalize_name

DAGS_DIR = Path("orchestration") / "dags"


@dataclass
class AddResult:
    name: str
    source: str
    files: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    dags: list[str] = field(default_factory=list)


def _require_platform(config: VectenConfig) -> None:
    if not config.has("platform"):
        raise Failure(
            ExitCode.REFUSED,
            "connectors belong to the platform component, which this project does not deploy",
        )


def _unknown(name: str) -> Failure:
    return Failure(
        ExitCode.REFUSED,
        f"{name} is neither a dlt verified source nor a connector of an installed extension; "
        "start from a generic source with --from rest|sql|filesystem, "
        "or let the connector skill write one",
    )


def _dag_files(
    project_root: Path,
    decl: ConnectorDecl,
    keep_existing: bool,
    declared_groups: dict[str, ExtensionGroup],
) -> dict[Path, str]:
    """`declared_groups` are an extension's own, whose schedules the DAG files carry; a group
    it does not declare runs on the default schedule."""
    files = {}
    for group, objects in decl.groups.items():
        path = project_root / DAGS_DIR / f"sync_{decl.name}_{group}.py"
        if path.exists():
            if keep_existing:
                continue
            raise Failure(
                ExitCode.REFUSED,
                f"{path.relative_to(project_root)} already exists; "
                f"move it aside to add {decl.name}",
            )
        declared = declared_groups.get(group, ExtensionGroup(objects))
        files[path] = render_dag_file(
            decl.name, group, objects, declared.schedule, declared.timezone
        )
    return files


def _stage_generic(stage: Path, name: str, kind: str, secret: str) -> ConnectorDecl:
    render_connector_files(stage, name, kind, source_function=None, secrets=[("", secret)])
    return ConnectorDecl(name=name, source=kind, secrets=[secret])


def _stage_extension(stage: Path, name: str, ext: ExtensionConnector) -> ConnectorDecl:
    try:
        ext.write(stage)
    except Exception as exc:
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"extension {ext.package} failed to write connector {name}: "
            f"{type(exc).__name__}: {exc}",
        ) from None
    if not (stage / "connector.py").is_file():
        raise Failure(
            ExitCode.TOOL_FAILED,
            f"extension {ext.package} wrote no connector.py for {name}",
        )
    if not (stage / "__init__.py").is_file():
        write_text_atomic(stage / "__init__.py", "")
    render_test_file(stage, name)
    decl = ConnectorDecl(
        name=name,
        source=ext.kind,
        secrets=list(ext.secrets),
        dependencies=list(ext.dependencies),
        extension=ext.package,
        version=ext.version or None,
    )
    if ext.groups:
        decl.groups = {group: list(spec.objects) for group, spec in ext.groups.items()}
    return decl


def _stage_verified(stage: Path, name: str, verified: VerifiedSources) -> ConnectorDecl:
    files = verified.materialize(name, stage / "source")
    init_py = stage / "source" / "__init__.py"
    function = source_function_name(init_py, name)
    pairs = [(param, secret_name(name, param)) for param in secret_params(init_py, name)]
    render_connector_files(stage, name, "dlt", source_function=function, secrets=pairs)
    return ConnectorDecl(
        name=name,
        source="dlt",
        secrets=[s for _, s in pairs],
        dependencies=files.requirements,
    )


def _stage_new(
    stage: Path,
    name: str,
    from_kind: str | None,
    verified: VerifiedSources,
    extensions: dict[str, ExtensionConnector],
) -> ConnectorDecl:
    if from_kind is not None:
        return _stage_generic(
            stage, name, from_kind, secret_name(name, GENERIC_SECRET_SUFFIX[from_kind])
        )
    if name in extensions:
        return _stage_extension(stage, name, extensions[name])
    if name in verified.names():
        return _stage_verified(stage, name, verified)
    raise _unknown(name)


def _check_declared(
    decl: ConnectorDecl, from_kind: str | None, extensions: dict[str, ExtensionConnector]
) -> None:
    """Refuse a re-render the declaration cannot drive, before anything is staged."""
    name = decl.name
    if from_kind is not None and from_kind != decl.source:
        raise Failure(
            ExitCode.USAGE,
            f"{name} is declared in vecten.toml as {decl.source!r}, not {from_kind!r}; "
            "drop --from to render it again from its declaration",
        )
    if decl.extension or decl.source == "vecten-managed":
        ext = extensions.get(name)
        if (
            ext is None
            or not decl.extension
            or normalize_name(ext.package) != normalize_name(decl.extension)
        ):
            raise Failure(
                ExitCode.REFUSED,
                f"{name} comes from extension {decl.extension or '(none recorded)'}, which is "
                "not listed under extensions in vecten.toml or does not provide it; list and "
                "install the extension, then run `vecten connector add` again",
            )
    elif decl.source == "custom":
        raise Failure(
            ExitCode.REFUSED,
            f"{name} is a custom connector: its source was written in the project, so restore "
            f"connectors/{name}/ from version control or let the connector skill write it again",
        )


def _stage_declared(
    stage: Path,
    decl: ConnectorDecl,
    verified: VerifiedSources,
    extensions: dict[str, ExtensionConnector],
) -> None:
    name = decl.name
    if decl.extension or decl.source == "vecten-managed":
        _stage_extension(stage, name, extensions[name])
    elif decl.source == "dlt":
        if name not in verified.names():
            raise Failure(
                ExitCode.REFUSED,
                f"{name} is declared as a dlt verified source, but dlt has none by that name",
            )
        _stage_verified(stage, name, verified)
    else:
        secret = (
            decl.secrets[0]
            if decl.secrets
            else secret_name(name, GENERIC_SECRET_SUFFIX[decl.source])
        )
        _stage_generic(stage, name, decl.source, secret)


def add_connector(
    project_root: Path,
    name: str,
    from_kind: str | None,
    verified: VerifiedSources,
    extensions: dict[str, ExtensionConnector],
) -> AddResult:
    """Add a new connector, or render a declared one whose directory is missing.

    A re-render keeps the declaration as written (access, secrets, dependencies, groups,
    extension, version) and refreshes only its scaffold hash.
    """
    check_connector_name(name)
    config = load_config(project_root)
    _require_platform(config)
    connector_dir = project_root / "connectors" / name
    if connector_dir.exists():
        raise Failure(
            ExitCode.REFUSED, f"connector {name} already exists; edit {connector_dir} instead"
        )
    if from_kind is not None and from_kind not in GENERIC_KINDS:
        raise Failure(
            ExitCode.USAGE,
            f"unknown generic source {from_kind!r}; one of: {', '.join(GENERIC_KINDS)}",
        )
    declared = config.connector(name)
    if declared is not None:
        _check_declared(declared, from_kind, extensions)

    connector_dir.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=connector_dir.parent) as tmp:
        stage = Path(tmp) / name
        stage.mkdir()
        if declared is None:
            decl = _stage_new(stage, name, from_kind, verified, extensions)
        else:
            _stage_declared(stage, declared, verified, extensions)
            decl = declared
        decl.scaffold_hash = scaffold_hash((stage / "connector.py").read_text(encoding="utf-8"))
        # Everything that can refuse runs before the move, so a refusal changes nothing.
        load_sources(project_root / "dbt")
        from_extension = (
            extensions.get(name) if decl.extension or decl.source == "vecten-managed" else None
        )
        dag_files = _dag_files(
            project_root,
            decl,
            keep_existing=declared is not None,
            declared_groups=from_extension.groups if from_extension is not None else {},
        )
        shutil.move(str(stage), str(connector_dir))

    for dag_path, text in dag_files.items():
        dag_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(dag_path, text)
    dags = [str(p.relative_to(project_root)) for p in dag_files]
    ensure_source(project_root / "dbt", name)
    if declared is None:
        config.wanted = [w for w in config.wanted if w != name]
        config.connectors.append(decl)
    save_config(project_root, config)
    files = sorted(
        str(p.relative_to(project_root)) for p in connector_dir.rglob("*") if p.is_file()
    )
    return AddResult(name, decl.source, files, list(decl.secrets), list(decl.dependencies), dags)
