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
        raise Failure(
            ExitCode.REFUSED, f"invalid or missing {path.name}; rerun vecten extension add"
        ) from None


def write_project_layer(stack_dir: Path, config: VectenConfig, registry: str | None) -> None:
    build_dir = stack_dir / BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    reqs = project_requirements(config)
    root = stack_dir.parent.parent
    inputs, lock = root / "requirements.txt", root / "requirements.lock"
    runtime = {}
    if inputs.exists() or lock.exists():
        if not inputs.is_file() or not lock.is_file():
            raise Failure(
                ExitCode.REFUSED, "project dependency records are stale; rerun vecten extension add"
            )
        direct = [
            requirement(line)
            for line in inputs.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        configured = [requirement(text) for text in reqs]
        if {canonicalize_name(r.name) for r in direct} != {
            canonicalize_name(r.name) for r in configured
        }:
            raise Failure(
                ExitCode.REFUSED, "project dependency records are stale; rerun vecten extension add"
            )
        for wanted in configured:
            for candidate in (
                r for r in direct if canonicalize_name(r.name) == canonicalize_name(wanted.name)
            ):
                pins = list(candidate.specifier)
                compatible = candidate.specifier == wanted.specifier or (
                    len(pins) == 1
                    and pins[0].operator == "=="
                    and pins[0].version in wanted.specifier
                )
                if candidate.extras != wanted.extras or not compatible:
                    raise Failure(
                        ExitCode.REFUSED,
                        "project dependency records are stale; rerun vecten extension add",
                    )
        reqs = lock_lines(lock, [*configured, *direct])
        for component, kind in (("platform", "airflow"), ("mcp", "mcp")):
            if component in config.components:
                runtime[kind] = lock_lines(
                    root / f"requirements-{kind}.lock", [*configured, *direct]
                )
    write_text_atomic(build_dir / "requirements.txt", "".join(f"{r}\n" for r in reqs))
    pins = {"airflow": f'"apache-airflow=={VERSIONS.airflow_version}" ', "mcp": ""}
    for kind, pin in pins.items():
        source = "requirements.txt"
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
            f"FROM {base_image(kind, registry)}\n{install if reqs else ''}",
        )
