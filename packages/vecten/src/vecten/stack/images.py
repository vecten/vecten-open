"""The thin per-project image layer over the vecten base images."""

from __future__ import annotations

import shlex
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from vecten_shared.config import VectenConfig
from vecten_shared.extensions import requirement
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic
from vecten_shared.requirements import parse_requirement
from vecten_shared.versions import VERSIONS

LOCAL_AIRFLOW = "vecten-airflow"
LOCAL_MCP = "vecten-mcp"
# Docker sends the whole build context to the daemon, so the context is a directory of its
# own holding the layer's inputs and none of the stack's secrets.
BUILD_DIR = "build"
BASE_CONSTRAINTS_SCRIPT = (
    "from importlib.metadata import distributions; "
    "print('\\n'.join(sorted(d.metadata['Name'] + '==' + d.version for d in distributions())))"
)


REQUIRED_PLATFORMS = ("linux/amd64", "linux/arm64")
ATTESTATION = "attestation-manifest"


def base_image(kind: str, registry: str | None, version: str = VERSIONS.vecten) -> str:
    name = LOCAL_AIRFLOW if kind == "airflow" else LOCAL_MCP
    prefix = f"{registry.rstrip('/')}/" if registry else ""
    return f"{prefix}{name}:{version}"


def missing_platforms(descriptor: dict) -> list[str]:
    """The required platforms a tag's descriptor from `imagetools inspect` lacks."""
    if "manifests" not in descriptor:
        raise ValueError("single-platform image")
    found = set()
    for entry in descriptor["manifests"]:
        if entry.get("annotations", {}).get("vnd.docker.reference.type") == ATTESTATION:
            continue
        platform = entry.get("platform", {})
        name = f"{platform.get('os')}/{platform.get('architecture')}"
        # arm64 has one variant, v8; registries list it with or without the suffix.
        if name == "linux/arm64" and platform.get("variant") not in (None, "v8"):
            continue
        found.add(name)
    return [p for p in REQUIRED_PLATFORMS if p not in found]


def project_requirements(config: VectenConfig) -> list[str]:
    reqs = set(config.extensions)
    for connector in config.connectors:
        reqs.update(connector.dependencies)
    # pip reads a requirements line starting with "-" as an option, which can point the
    # install at another index or at local code.
    for req in sorted(reqs):
        if req.lstrip().startswith("-"):
            raise Failure(
                ExitCode.USAGE,
                f"requirement {req!r} looks like a pip option; extensions and connector "
                "dependencies must be package specifiers",
            )
    return sorted(reqs)


def lock_lines(path: Path, wanted: list[Requirement]) -> list[str]:
    try:
        lines = [
            line
            for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        parsed = [Requirement(line) for line in lines]
        if any(
            r.url
            or len(r.specifier) != 1
            or any(spec.operator != "==" or "*" in spec.version for spec in r.specifier)
            for r in parsed
        ):
            raise InvalidRequirement()
        for req in wanted:
            candidates = [
                r for r in parsed if canonicalize_name(r.name) == canonicalize_name(req.name)
            ]
            if not candidates or any(
                next(iter(r.specifier)).version not in req.specifier for r in candidates
            ):
                raise InvalidRequirement()
        return lines
    except OSError, InvalidRequirement:
        message = f"invalid or missing {path.name}; run `vecten lock`"
        raise Failure(ExitCode.REFUSED, message) from None


STALE = "project dependency records are stale; run `vecten lock`"
RUNTIME = (("platform", "airflow"), ("mcp", "mcp"))


def _legacy_lines(
    root: Path, config: VectenConfig, reqs: list[str]
) -> tuple[list[str], list[Requirement]]:
    """Locks an earlier release wrote, without inputs lines: checked name by name.

    Returns the CLI lock's lines and every requirement a runtime lock must also satisfy.
    """
    if any(config.dependencies.python(c) for c in ("platform", "mcp")):
        # An earlier release never resolved these, so its locks cannot hold them.
        raise Failure(
            ExitCode.REFUSED,
            "[dependencies] Python requirements need locks written by `vecten lock`; "
            "run `vecten lock`",
        )
    inputs, lock = root / "requirements.txt", root / "requirements.lock"
    if not inputs.is_file() or not lock.is_file():
        raise Failure(ExitCode.REFUSED, STALE)
    direct = [
        requirement(line)
        for line in inputs.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    configured = [requirement(text) for text in reqs]
    if {canonicalize_name(r.name) for r in direct} != {
        canonicalize_name(r.name) for r in configured
    }:
        raise Failure(ExitCode.REFUSED, STALE)
    for wanted in configured:
        for candidate in (
            r for r in direct if canonicalize_name(r.name) == canonicalize_name(wanted.name)
        ):
            pins = list(candidate.specifier)
            compatible = candidate.specifier == wanted.specifier or (
                len(pins) == 1 and pins[0].operator == "==" and pins[0].version in wanted.specifier
            )
            if candidate.extras != wanted.extras or not compatible:
                raise Failure(ExitCode.REFUSED, STALE)
    wanted = [*configured, *direct]
    return lock_lines(lock, wanted), wanted


def _wanted(root: Path, config: VectenConfig, component: str) -> list[Requirement]:
    """What a fresh stamped lock must pin: every requirement among its recorded inputs."""
    from vecten import locking

    record = locking.read_record(root) or {}
    roots = locking.roots(config, locking.read_pins(root), record, component) or []
    wanted = []
    for text in roots:
        req = parse_requirement(text)
        if req is not None:
            wanted.append(req)
    return wanted


def write_project_layer(stack_dir: Path, config: VectenConfig, registry: str | None) -> None:
    from vecten import locking

    build_dir = stack_dir / BUILD_DIR
    reqs = project_requirements(config)
    root = stack_dir.parent.parent
    runtime = {}
    records = [locking.PINS_FILE, locking.RECORD_FILE, *locking.LOCK_FILES.values()]
    if not any((root / name).exists() for name in records):
        if any(config.dependencies.python(c) for c in locking.LOCK_FILES):
            raise Failure(
                ExitCode.REFUSED,
                "[dependencies] Python requirements install only from locks; run `vecten lock`",
            )
    else:
        legacy = None
        for component, kind in RUNTIME:
            if component not in config.components:
                continue
            name = locking.LOCK_FILES[component]
            state = locking.lock_state(root, config, component)
            if state == "unstamped" and not (root / locking.RECORD_FILE).exists():
                legacy = legacy if legacy is not None else _legacy_lines(root, config, reqs)
                runtime[kind] = lock_lines(root / name, legacy[1])
            elif state == "fresh":
                # The stamp proves the inputs are unchanged, not that the body still pins them.
                runtime[kind] = lock_lines(root / name, _wanted(root, config, component))
            else:
                raise Failure(
                    ExitCode.REFUSED,
                    f"{name} is {state}: it does not match vecten.toml, requirements.txt and "
                    f"{locking.RECORD_FILE}; run `vecten lock`",
                )
        reqs = legacy[0] if legacy is not None else []
    build_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(build_dir / "requirements.txt", "".join(f"{r}\n" for r in reqs))
    pins = {"airflow": f'"apache-airflow=={VERSIONS.airflow_version}" ', "mcp": ""}
    for kind, pin in pins.items():
        source, lines = "requirements.txt", runtime.get(kind, reqs)
        if kind in runtime:
            source = f"requirements-{kind}.txt"
            write_text_atomic(build_dir / source, "".join(f"{r}\n" for r in runtime[kind]))
        mount = (
            "--mount=type=secret,id=package_index,required=true,mode=0444 "
            if config.packages.private_index
            else ""
        )
        secret = (
            'PIP_INDEX_URL="$(cat /run/secrets/package_index)" '
            if config.packages.private_index
            else ""
        )
        diagnostic = (
            "echo 'dependency installation failed'; "
            if config.packages.private_index
            else "cat /tmp/vecten-pip.log; "
        )
        # Keep every package supplied by the base image; a changed base must never be silently
        # downgraded to satisfy a lock generated against a different environment.
        install = (
            f"COPY {source} /tmp/requirements.txt\n"
            f"RUN {mount}python -c {shlex.quote(BASE_CONSTRAINTS_SCRIPT)} "
            "> /tmp/vecten-base-constraints.txt "
            f"&& {{ {secret}pip install --no-cache-dir {pin}"
            "--constraint /tmp/vecten-base-constraints.txt "
            "-r /tmp/requirements.txt; } > /tmp/vecten-pip.log 2>&1 "
            f"|| {{ {diagnostic}rm -f /tmp/vecten-pip.log "
            "/tmp/vecten-base-constraints.txt; exit 1; }; "
            "rm -f /tmp/vecten-pip.log /tmp/vecten-base-constraints.txt\n"
        )
        write_text_atomic(
            build_dir / f"{kind}.Dockerfile",
            f"FROM {base_image(kind, registry)}\n{install if lines else ''}",
        )
