"""The thin per-project image layer over the vecten base images."""

from __future__ import annotations

import shlex
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from vecten_shared.config import VectenConfig
from vecten_shared.extension_record import RECORD_FILE
from vecten_shared.extensions import requirement
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic
from vecten_shared.requirements import parse_requirement
from vecten_shared.versions import VERSIONS, normalize_name

LOCAL_AIRFLOW = "vecten-airflow"
LOCAL_MCP = "vecten-mcp"
# Docker sends the whole build context to the daemon, so the context is a directory of its
# own holding the layer's inputs and none of the stack's secrets.
BUILD_DIR = "build"
# The directory, beside the build context, where `up` keeps the git sources it fetched.
SOURCES_DIR = "sources"
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


def lock_lines(
    path: Path, wanted: list[Requirement], sources: frozenset[str] = frozenset()
) -> list[str]:
    """The index pins `path` holds, every one of `wanted` among them; a line of `sources`, a git
    source the record holds, is left to the install from its bundled source."""
    try:
        lines = [
            line
            for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#") and line not in sources
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
BUILD_CONSTRAINTS = "build-constraints.txt"


def _in_repo_extensions(root: Path) -> list:
    """The recorded in-repo extensions, each checked again against the folder rules."""
    from pathlib import PurePosixPath

    from vecten import in_repo
    from vecten_shared.extension_record import read_record

    local = [e for e in (read_record(root) or {}).values() if e.source_path]
    for entry in local:
        in_repo.read(root, PurePosixPath(entry.source_path))
    return sorted(local, key=lambda e: e.source_path)


def _git_sources(root: Path, config: VectenConfig) -> dict[str, str]:
    """The record line of each registered git-sourced extension, by name, as
    `requirements.txt` and every lock hold it; the build gets its source under `sources`."""
    from vecten import git_extensions
    from vecten_shared.extension_record import read_record

    registered = git_extensions.registered_sources(config, read_record(root) or {})
    return {
        name: git_extensions.line_for(name, extras, entry.git)
        for name, (extras, entry) in registered.items()
    }


RUNTIME = (("platform", "airflow"), ("mcp", "mcp"))
SOURCES = "/tmp/sources"
VERIFY = f"python -m vecten_shared.git_sources verify {SOURCES} /tmp/{RECORD_FILE} && "


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


# The user each base image runs as; the operating-system step switches back to it.
IMAGE_USERS = {"airflow": "airflow", "mcp": "app"}


def _system_step(config: VectenConfig, kind: str) -> str:
    component = "platform" if kind == "airflow" else "mcp"
    packages = config.dependencies.system(component) if config.has(component) else []
    if not packages:
        return ""
    return (
        "USER root\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends "
        f"{' '.join(packages)} && rm -rf /var/lib/apt/lists/* "
        # apt falls back to reading an unknown name as a pattern; each listed name must be the
        # package that was installed.
        f"&& dpkg -s {' '.join(p.split('=')[0] for p in packages)} >/dev/null\n"
        f"USER {IMAGE_USERS[kind]}\n"
    )


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


def volume_step(config: VectenConfig, root: Path, kind: str) -> str:
    """Create each declared service volume path owned by the image's runtime user.

    Docker seeds an empty named volume with the ownership of the image's directory at the mount
    path, so a path the image lacks would come up owned by root and unwritable.
    """
    if not config.services:
        return ""
    from vecten_shared.extension_record import read_record
    from vecten_shared.services import bound_services

    component = "platform" if kind == "airflow" else "mcp"
    paths = sorted(
        {
            bound.service.volume
            for bound in bound_services(config, read_record(root) or {})
            if bound.host == component and bound.service.volume is not None
        }
    )
    if not paths:
        return ""
    # A path the image already holds, such as /etc, keeps its owner: handing it to the runtime
    # user would let every container of this image rewrite it.
    user, quoted = IMAGE_USERS[kind], " ".join(shlex.quote(p) for p in paths)
    return (
        f'USER root\nRUN for p in {quoted}; do [ -e "$p" ] || '
        f'{{ mkdir -p "$p" && chown {user} "$p"; }}; done\nUSER {user}\n'
    )


def write_project_layer(stack_dir: Path, config: VectenConfig, registry: str | None) -> None:
    from vecten import locking

    build_dir = stack_dir / BUILD_DIR
    reqs = project_requirements(config)
    root = stack_dir.parent.parent
    runtime = {}
    legacy = None
    records = [locking.PINS_FILE, locking.RECORD_FILE, *locking.LOCK_FILES.values()]
    records_exist = any((root / name).exists() for name in records)
    if not records_exist:
        from vecten import in_repo

        local = [
            r
            for r in map(parse_requirement, config.extensions)
            if r and in_repo.has_folder(root, normalize_name(r.name))
        ]
        if local:
            raise Failure(
                ExitCode.REFUSED,
                f"{local[0].name} is kept in extensions/ and installs only from locks; "
                "run `vecten lock`",
            )
        if any(config.dependencies.python(c) for c in locking.LOCK_FILES):
            raise Failure(
                ExitCode.REFUSED,
                "[dependencies] Python requirements install only from locks; run `vecten lock`",
            )
    else:
        problems = locking.git_problems(root, config)
        if problems:
            raise Failure(
                ExitCode.REFUSED,
                "; ".join(problems) + "; record each git source again with "
                "`vecten extension add` and its tag",
            )
        sources = frozenset(_git_sources(root, config).values())
        for component, kind in RUNTIME:
            if component not in config.components:
                continue
            name = locking.LOCK_FILES[component]
            state = locking.lock_state(root, config, component)
            if state == "unstamped" and not (root / locking.RECORD_FILE).exists():
                legacy = legacy if legacy is not None else _legacy_lines(root, config, reqs)
                runtime[kind] = lock_lines(root / name, legacy[1], sources)
            elif state == "fresh":
                # The stamp proves the inputs are unchanged, not that the body still pins them.
                runtime[kind] = lock_lines(root / name, _wanted(root, config, component), sources)
            else:
                raise Failure(
                    ExitCode.REFUSED,
                    f"{name} is {state}: it does not match vecten.toml, requirements.txt and "
                    f"{locking.RECORD_FILE}; run `vecten lock`",
                )
        reqs = legacy[0] if legacy is not None else []
    # A stamped lock is the full third-party closure, so it installs without resolving and an
    # in-repo extension's name is never looked up on an index.
    closure = bool(runtime) and legacy is None
    local = _in_repo_extensions(root) if closure else []
    fetched = list(_git_sources(root, config)) if closure else []
    build_dir.mkdir(parents=True, exist_ok=True)
    constraints = build_dir / BUILD_CONSTRAINTS
    if local or fetched:
        from vecten_shared.extension_record import read_record

        record = read_record(root) or {}
        entries = [*local, *(record[name] for name in fetched)]
        build = sorted({line for entry in entries for line in entry.build_requires})
        write_text_atomic(constraints, "".join(f"{line}\n" for line in build))
    else:
        constraints.unlink(missing_ok=True)
    # The build checks each bundled source against the record the bundle carries.
    bundled_record = build_dir / RECORD_FILE
    if fetched:
        write_text_atomic(bundled_record, (root / RECORD_FILE).read_text(encoding="utf-8"))
    else:
        bundled_record.unlink(missing_ok=True)
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
        no_deps = "--no-deps " if closure else ""
        copies = f"COPY {source} /tmp/requirements.txt\n"
        extra = verify = ""
        if local or fetched:
            copies += f"COPY {BUILD_CONSTRAINTS} /tmp/{BUILD_CONSTRAINTS}\n"
            paths = " ".join(
                [
                    *(f"/tmp/extensions/{e.source_path.split('/', 1)[1]}" for e in local),
                    *(f"{SOURCES}/{name}" for name in fetched),
                ]
            )
            if local:
                mount += "--mount=type=bind,from=extensions,target=/tmp/extensions,rw "
            if fetched:
                # Bind mounts, so neither the source tree nor the record stays in a layer.
                mount += (
                    f"--mount=type=bind,from=sources,target={SOURCES},rw "
                    f"--mount=type=bind,source={RECORD_FILE},"
                    f"target=/tmp/{RECORD_FILE} "
                )
                verify = VERIFY
            extra = (
                f" && {secret}pip install --no-cache-dir --no-deps "
                f"--build-constraint /tmp/{BUILD_CONSTRAINTS} {paths} && pip check"
            )
        # Keep every package supplied by the base image; a changed base must never be silently
        # downgraded to satisfy a lock generated against a different environment.
        install = (
            copies + f"RUN {mount}{verify}python -c {shlex.quote(BASE_CONSTRAINTS_SCRIPT)} "
            "> /tmp/vecten-base-constraints.txt "
            f"&& {{ {secret}pip install --no-cache-dir {no_deps}{pin}"
            "--constraint /tmp/vecten-base-constraints.txt "
            f"-r /tmp/requirements.txt{extra}; }} > /tmp/vecten-pip.log 2>&1 "
            f"|| {{ {diagnostic}rm -f /tmp/vecten-pip.log "
            "/tmp/vecten-base-constraints.txt; exit 1; }; "
            "rm -f /tmp/vecten-pip.log /tmp/vecten-base-constraints.txt\n"
        )
        write_text_atomic(
            build_dir / f"{kind}.Dockerfile",
            f"FROM {base_image(kind, registry)}\n"
            f"{_system_step(config, kind)}{install if lines or local or fetched else ''}"
            f"{volume_step(config, root, kind)}",
        )
