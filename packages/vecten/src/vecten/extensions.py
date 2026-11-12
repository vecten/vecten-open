"""Install extensions and record portable, credential-free project dependencies."""

import importlib
import json
import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import metadata
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import tomlkit
from pydantic import ValidationError

from vecten import git_extensions, in_repo, locking
from vecten.stack.images import BASE_CONSTRAINTS_SCRIPT, base_image
from vecten_shared import git_sources
from vecten_shared.config import VectenConfig, label_problem, load_config, service_hostname
from vecten_shared.declarations import (
    Manifest,
    check_settings,
    component_for,
    environments_named,
    parse_manifest,
)
from vecten_shared.extensions import (
    DECLARATION_GROUPS,
    GROUPS,
    requirement,
)
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic
from vecten_shared.requirements import REQUIREMENT_RULE, parse_requirement
from vecten_shared.services import GROUP as SERVICES_GROUP
from vecten_shared.services import Declaration, check_bindings, parse_service
from vecten_shared.versions import VERSIONS, normalize_name


def package_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    config = load_config(root)
    if getattr(getattr(config, "packages", None), "private_index", False):
        from vecten_shared.provider.local import LocalProvider

        value = LocalProvider(root, inside_compose=False).get_secret("PACKAGE_INDEX_URL")
        if not value:
            raise Failure(ExitCode.PREREQUISITE, "set PACKAGE_INDEX_URL with vecten secret set")
        env["UV_INDEX_URL"] = value
        env["PIP_INDEX_URL"] = value
    return env


def uv_environment(root: Path, served: Mapping[str, Path] | None = None) -> dict[str, str]:
    """The environment uv and the build backends it runs get: never a git credential, and
    with git sources, a git that reads each one from the repository serving its URL."""
    return git_extensions.uv_settings(package_environment(root), served or {})


def _uv(
    args: list[str],
    root: Path,
    cwd: Path | None = None,
    served: Mapping[str, Path] | None = None,
) -> None:
    if not shutil.which("uv"):
        raise Failure(ExitCode.PREREQUISITE, "install uv to manage extension dependencies")
    env = uv_environment(root, served)
    try:
        result = subprocess.run(
            ["uv", "--no-config", *args],
            env=env,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except OSError:
        raise Failure(
            ExitCode.TOOL_FAILED, "could not start the extension package manager"
        ) from None
    if result.returncode:
        message = (
            "extension package resolution or installation failed; "
            "check the package version and index access"
        )
        from urllib.parse import urlsplit

        private = load_config(root).packages.private_index or any(
            urlsplit(value).password for key, value in env.items() if key.endswith("INDEX_URL")
        )
        if not private:
            detail = re.sub(r"https?://\S+", "[URL redacted]", result.stderr or result.stdout)
            message += "\n" + detail[-4000:].strip()
        raise Failure(ExitCode.TOOL_FAILED, message)


def release_constraints(path: Path) -> Path:
    path.write_text(
        "".join(
            f"{name}=={VERSIONS.vecten}\n"
            for name in ("vecten", "vecten-shared", "vecten-platform", "vecten-mcp")
        )
    )
    return path


@dataclass(frozen=True)
class PackageMetadata:
    name: str
    version: str
    entry_points: tuple
    # The extras the wheel provides (`Provides-Extra`), read without importing it.
    extras: tuple[str, ...] = ()
    # Read from the project's extensions/<name>/ rather than staged from an index.
    in_repo: bool = False
    # Fetched from a git source, whose copy outlives the lock that installs it.
    git: git_extensions.Copy | None = None


def inspect_package(package: str, root: Path) -> PackageMetadata:
    """Stage package metadata without changing the environment running the CLI."""
    req = requirement(package)
    with tempfile.TemporaryDirectory(prefix="vecten-inspect-") as temp:
        site = Path(temp) / "site"
        constraints = release_constraints(Path(temp) / "constraints.txt")
        _uv(
            [
                "pip",
                "install",
                "--target",
                str(site),
                "--no-deps",
                "--constraint",
                str(constraints),
                str(req),
            ],
            root,
        )
        for dist in metadata.distributions(path=[str(site)]):
            if normalize_name(dist.name) == normalize_name(req.name):
                extras = sorted({e.lower() for e in dist.metadata.get_all("Provides-Extra") or ()})
                return PackageMetadata(
                    dist.name, dist.version, tuple(dist.entry_points), tuple(extras)
                )
    raise Failure(ExitCode.TOOL_FAILED, f"could not inspect package {req.name}")


def base_constraints(kind: str) -> str:
    image = base_image(kind, os.environ.get("VECTEN_IMAGE_REGISTRY"))
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--entrypoint",
                "python",
                image,
                "-c",
                BASE_CONSTRAINTS_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError, subprocess.TimeoutExpired:
        raise Failure(
            ExitCode.PREREQUISITE,
            "Docker and local runtime images are required to lock extension dependencies",
        ) from None
    if result.returncode:
        raise Failure(
            ExitCode.PREREQUISITE,
            f"make runtime image {image} available locally before adding extensions; "
            "build it with vecten dev build-images or pull the released image "
            "and set VECTEN_IMAGE_REGISTRY",
        )
    # Metadata names and versions, never direct install URLs or container environment values.
    for line in result.stdout.splitlines():
        requirement(line)
    return result.stdout


def site_packages() -> Path:
    return Path(sysconfig.get_paths()["purelib"])


def install_lock(lock: str, root: Path, editable: tuple[str, ...] | list[str] = ()) -> None:
    """Install the CLI lock, then each in-repo extension editable, all without resolving.

    The lock is the full third-party closure, so `--no-deps` installs exactly its pins and never
    looks a name up on an index, which an in-repo extension's name may also exist on. A git
    source's line is left to `install_sources`, which installs it from its fetched copy.
    """
    with tempfile.TemporaryDirectory(prefix="vecten-install-") as temp:
        path = Path(temp) / "requirements.lock"
        path.write_text(
            "".join(f"{line}\n" for line in lock.splitlines() if not git_extensions.is_source(line))
        )
        constraints = release_constraints(Path(temp) / "constraints.txt")
        install = ["pip", "install", "--python", sys.executable, "--no-deps"]
        _uv([*install, "--constraint", str(constraints), "-r", str(path)], root)
        for relative in editable:
            _uv([*install, "--editable", str(root / relative)], root)
    # An editable install is a path file, which Python reads only at start; the manifests and
    # services this process loads next import from those paths.
    if editable:
        site.addsitedir(str(site_packages()))
    importlib.invalidate_caches()


def install_sources(
    copies: Mapping[str, git_extensions.Copy], record: dict[str, locking.Extension], root: Path
) -> None:
    """Install each git-sourced extension from its fetched copy, not editable and without
    resolving, its build backend held to the versions the record pins."""
    with tempfile.TemporaryDirectory(prefix="vecten-install-") as temp:
        for name, copy in sorted(copies.items()):
            constraints = Path(temp) / f"{name}-build-constraints.txt"
            constraints.write_text("".join(f"{line}\n" for line in record[name].build_requires))
            _uv(
                [
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "--no-deps",
                    "--build-constraint",
                    str(constraints),
                    str(copy.directory),
                ],
                root,
            )
    importlib.invalidate_caches()


def resolve_lock(
    requirements: list[str],
    root: Path,
    *,
    constraints: str = "",
    runtime: bool = False,
    previous: str | None = None,
    upgrade: tuple[str, ...] = (),
    cwd: Path | None = None,
    sources: Mapping[str, tuple[git_extensions.Copy, str]] | None = None,
    served: Mapping[str, Path] | None = None,
) -> str:
    """Resolve `requirements`, keeping every pin of `previous` that still satisfies them.

    A `./extensions/<name>` input resolves against `cwd`; uv echoes its line as given. Each
    of `sources`, by name, is a git source's fetched copy and its record line, and `served`
    the repository serving each source URL: uv reads the sources from those, and a source's
    line in the output is replaced by its record line.
    """
    sources = sources or {}
    with tempfile.TemporaryDirectory(prefix="vecten-lock-") as temp:
        source, lock = Path(temp) / "requirements.txt", Path(temp) / "requirements.lock"
        source.write_text("\n".join(requirements) + "\n")
        # uv reads an existing output file as preferences, so a re-lock moves only what the
        # changed inputs or an explicit upgrade require.
        if previous is not None:
            lock.write_text(previous)
        constraint_file = release_constraints(Path(temp) / "constraints.txt")
        constraint_file.write_text(constraint_file.read_text() + constraints)
        _uv(
            [
                "pip",
                "compile",
                str(source),
                "--constraint",
                str(constraint_file),
                "--python-version",
                VERSIONS.python,
                *(["--python-platform", "linux"] if runtime else ["--universal"]),
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(lock),
                *(arg for name in upgrade for arg in ("--upgrade-package", name)),
            ],
            root,
            cwd=cwd,
            served=served,
        )
        text = lock.read_text()
        from packaging.requirements import InvalidRequirement, Requirement

        lines = []
        try:
            for line in text.splitlines():
                if not line.strip() or line.startswith("#") or line.startswith(IN_REPO_PREFIX):
                    lines.append(line)
                    continue
                req = Requirement(line)
                if req.url:
                    # Only a registered extension may come from git, and only from its record.
                    copy, record_line = sources.get(normalize_name(req.name), (None, None))
                    if copy is None or not git_extensions.same_source(line, copy.origin):
                        raise InvalidRequirement()
                    lines.append(record_line)
                    continue
                if (
                    not all(s.operator == "==" and "*" not in s.version for s in req.specifier)
                    or not req.specifier
                ):
                    raise InvalidRequirement()
                lines.append(line)
        except InvalidRequirement:
            raise Failure(
                ExitCode.REFUSED, "package resolution produced a non-portable dependency lock"
            ) from None
        return "".join(f"{line}\n" for line in lines)


def write_project_files(files: dict[Path, str | Path | None]) -> None:
    before = {}
    for path, content in files.items():
        if path.is_symlink():
            if isinstance(content, str):
                raise Failure(ExitCode.REFUSED, f"refusing to replace symbolic link {path.name}")
            before[path] = Path(os.readlink(path))
        else:
            before[path] = path.read_bytes() if path.exists() else None
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, Path):
                if path.is_symlink():
                    path.unlink()
                path.symlink_to(content, target_is_directory=True)
            elif content is None:
                path.unlink(missing_ok=True)
            else:
                write_text_atomic(path, content)
    except OSError:
        for path, content in before.items():
            if path.is_symlink():
                path.unlink()
            if content is None:
                path.unlink(missing_ok=True)
            elif isinstance(content, Path):
                path.symlink_to(content, target_is_directory=True)
            else:
                path.write_bytes(content)
        raise Failure(
            ExitCode.TOOL_FAILED, "could not write extension records; project files restored"
        ) from None


RUNTIME_KINDS = {"platform": "airflow", "mcp": "mcp"}


def _pinned_version(text: str) -> str | None:
    specs = list(requirement(text).specifier)
    return specs[0].version if len(specs) == 1 and specs[0].operator == "==" else None


def _extension_pins(
    root: Path, config: VectenConfig, added: PackageMetadata | None, fetched: Path
) -> tuple[dict[str, str], dict[str, locking.Extension], dict[str, git_extensions.Copy]]:
    """Each registered extension's exact pin, record entry and, for a git source, its copy
    fetched into `fetched`, inspecting only what is unknown.

    `requirements.txt` may still hold an earlier release's connector dependencies; only the
    registered extensions' lines are kept.
    """
    pins = locking.read_pins(root)
    record = locking.read_record(root) or {}
    paths = _path_lines(root)
    found_pins, found_record, copies = {}, {}, {}
    credentials = None
    for registration in config.extensions:
        req = requirement(registration)
        name = normalize_name(req.name)
        extras = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
        added_here = added is not None and normalize_name(added.name) == name
        # A folder in extensions/ makes the name in-repo even when requirements.txt lost its
        # line, so the name is never looked up on an index, where a stranger may own it.
        local = name in paths or in_repo.has_folder(root, name)
        if (added_here and added.in_repo) or (not added_here and local):
            info = in_repo.read(root, PurePosixPath(in_repo.EXTENSIONS_DIR, name))
            found_pins[name] = f"./{info.path}{extras}"
            found_record[name] = _in_repo_entry(root, info, record.get(name))
            continue
        entry = record.get(name)
        if added_here and added.git is not None:
            copy = added.git
        elif not added_here and (
            git_extensions.is_source(pins.get(name, ""))
            or (entry is not None and entry.git is not None and name not in pins)
        ):
            if credentials is None:
                credentials = git_extensions.credentials(root)
            copy = _fetch_recorded(name, pins.get(name), entry, credentials, fetched)
        else:
            copy = None
        if copy is not None:
            found_pins[name] = git_extensions.line_for(name, req.extras, copy.origin)
            found_record[name] = _git_entry(root, copy, entry)
            copies[name] = copy
            continue
        if added_here:
            dist = added
        else:
            pinned = pins.get(name)
            version = _pinned_version(pinned) if pinned else None
            satisfied = version is not None and (not req.specifier or version in req.specifier)
            if (
                satisfied
                and entry
                and not entry.source_path
                and not entry.git
                and version == entry.version
            ):
                found_pins[name], found_record[name] = f"{name}{extras}=={entry.version}", entry
                continue
            dist = inspect_package(pinned if satisfied else registration, root)
        found_pins[name] = f"{name}{extras}=={dist.version}"
        found_record[name] = locking.Extension(version=dist.version, extras=tuple(dist.extras))
    return found_pins, found_record, copies


def _fetch_recorded(
    name: str,
    line: str | None,
    entry: locking.Extension | None,
    credentials: git_sources.Credentials,
    fetched: Path,
) -> git_extensions.Copy:
    """The copy of the commit `requirements.txt` records for `name`, checked against the
    record's source hash when the record holds that commit. No tag is resolved again."""
    if line is None:
        raise Failure(
            ExitCode.REFUSED,
            f"{locking.PINS_FILE} lost the git source of {name}; record it again with "
            "`vecten extension add` and its git source",
        )
    pinned = git_extensions.parse_line(line, locking.PINS_FILE)
    source = pinned.source
    same = (
        entry is not None
        and entry.git is not None
        and (entry.git.url, entry.git.commit, entry.git.subdirectory)
        == (source.url, pinned.commit, source.subdirectory)
    )
    # Whether the host answers without a credential is asked once per recorded commit.
    public = entry.git.public if same else git_sources.resolve(source, credentials).public
    copy = git_extensions.fetch_copy(
        source, pinned.commit, pinned.tag, public, credentials, fetched
    )
    if same and copy.source_sha256 != entry.source_sha256:
        raise Failure(
            ExitCode.REFUSED,
            f"{name} at {pinned.commit}: the fetched source has source_sha256 "
            f"{copy.source_sha256}, but {locking.RECORD_FILE} records {entry.source_sha256}",
        )
    return copy


def _build_pins(root: Path, requires: tuple[str, ...]) -> tuple[str, ...]:
    text = resolve_lock(list(requires), root, runtime=True, cwd=root)
    return tuple(line for line in text.splitlines() if line and not line.startswith("#"))


def _git_entry(
    root: Path, copy: git_extensions.Copy, previous: locking.Extension | None
) -> locking.Extension:
    """The record entry, with build requirements pinned once per source."""
    if previous is not None and previous.git and previous.source_sha256 == copy.source_sha256:
        build = previous.build_requires
    else:
        build = _build_pins(root, copy.metadata.build_requires)
    return locking.Extension(
        version=copy.metadata.version,
        extras=copy.metadata.extras,
        git=copy.origin,
        source_sha256=copy.source_sha256,
        build_requires=build,
    )


IN_REPO_PREFIX = f"./{in_repo.EXTENSIONS_DIR}/"


def _path_lines(root: Path) -> set[str]:
    """The in-repo extensions `requirements.txt` lists, by name."""
    path = root / locking.PINS_FILE
    if not path.is_file():
        return set()
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(IN_REPO_PREFIX):
            found.add(normalize_name(line[len(IN_REPO_PREFIX) :].split("[")[0].strip("/")))
    return found


def _in_repo_entry(
    root: Path, info: in_repo.InRepo, previous: locking.Extension | None
) -> locking.Extension:
    """The record entry, with build requirements pinned once per pyproject.toml."""
    if previous is not None and previous.metadata_sha256 == info.metadata_sha256:
        build = previous.build_requires
    else:
        build = _build_pins(root, info.build_requires)
    return locking.Extension(
        version=info.version,
        extras=info.extras,
        source_path=info.path,
        metadata_sha256=info.metadata_sha256,
        build_requires=build,
    )


def _check_connector_dependencies(config: VectenConfig) -> None:
    # Each one becomes a line of uv's input file, where a URL, a marker or a newline-borne
    # option would change what is resolved and installed into the CLI's environment.
    for connector in config.connectors:
        for dependency in connector.dependencies:
            if parse_requirement(dependency) is None:
                raise Failure(
                    ExitCode.CONFIG,
                    f"connector {connector.name}: dependency {dependency!r}: {REQUIREMENT_RULE}",
                )


def lock_project(
    root: Path,
    config: VectenConfig,
    *,
    upgrade: tuple[str, ...] = (),
    added: PackageMetadata | None = None,
) -> tuple[dict[Path, str], dict]:
    """Resolve every component's lock and install the CLI's; return the records to write.

    Nothing is written here: the caller writes these files together, so a failure in any
    resolution leaves every committed record as it was. Git sources are fetched into a
    directory that is removed when this returns.
    """
    _check_connector_dependencies(config)
    with tempfile.TemporaryDirectory(prefix="vecten-sources-") as fetched:
        return _lock_project(root, config, upgrade, added, Path(fetched))


def _lock_project(
    root: Path,
    config: VectenConfig,
    upgrade: tuple[str, ...],
    added: PackageMetadata | None,
    fetched: Path,
) -> tuple[dict[Path, str], dict]:
    pins, record, copies = _extension_pins(root, config, added, fetched)
    sources = {name: (copy, pins[name]) for name, copy in copies.items()}
    served = git_extensions.serve_copies(copies.values(), fetched)
    locks = {}
    for component in locking.components(config):
        lock_roots = locking.roots(config, pins, record, component)
        name = locking.LOCK_FILES[component]
        previous = root / name
        try:
            text = resolve_lock(
                lock_roots,
                root,
                constraints=(
                    base_constraints(RUNTIME_KINDS[component]) if component != "cli" else ""
                ),
                runtime=component != "cli",
                previous=previous.read_text() if previous.is_file() else None,
                upgrade=upgrade,
                cwd=root,
                sources=sources,
                served=served,
            )
        except Failure as failure:
            raise Failure(failure.code, f"{name}: {failure.message}") from None
        digest = locking.inputs_hash(component, [*lock_roots, *locking.metadata_markers(record)])
        lines = [
            line
            for line in text.splitlines()
            if line and not line.startswith("#") and not line.startswith(IN_REPO_PREFIX)
        ]
        _refuse_index_pins_of_in_repo_names(name, lines, record)
        body = "".join(f"{line}\n" for line in lines)
        locks[component] = f"{locking.stamp_line(component, digest)}\n{body}"
    install_lock(locks["cli"], root, [e.source_path for e in record.values() if e.source_path])
    if copies:
        install_sources(copies, record, root)
    manifests = load_manifests(config)
    services = load_services(config)
    record = {
        name: replace(
            entry,
            secrets=manifests[name].secrets if name in manifests else (),
            settings=manifests[name].settings if name in manifests else None,
            services={s: d.service for s, d in services.get(name, {}).items()},
        )
        for name, entry in record.items()
    }
    for env in environments_named(config):
        check_settings(config, record, env)
    check_bindings(config, record)
    from vecten.extension_skills import skill_artifacts

    files = {
        root / locking.PINS_FILE: "".join(f"{pin}\n" for pin in sorted(pins.values())),
        root / locking.RECORD_FILE: locking.record_text(record),
        **{root / locking.LOCK_FILES[c]: text for c, text in locks.items()},
        **skill_artifacts(root, config),
    }
    report = {
        "locks": [locking.LOCK_FILES[c] for c in locks],
        "extensions": {name: entry.version for name, entry in sorted(record.items())},
        "divergence": locking.divergence(locks),
        **_secrets_report(root, config, record),
        "unsupported": _unsupported(manifests, services),
    }
    return files, report


def _refuse_index_pins_of_in_repo_names(
    lock: str, lines: list[str], record: dict[str, locking.Extension]
) -> None:
    # Installing such a pin would take a stranger's package of the same name from an index.
    local = {name for name, entry in record.items() if entry.source_path}
    for line in lines:
        req = parse_requirement(line)
        if req is not None and normalize_name(req.name) in local:
            raise Failure(
                ExitCode.REFUSED,
                f"{lock}: the resolution pins {req.name} from a package index, but "
                f"{req.name} is an in-repo extension; rename it or the dependency on it",
            )


def declared_secrets(config: VectenConfig, record: dict[str, locking.Extension]) -> list[str]:
    """Every secret the project and its locked extensions declare."""
    names = set(config.secrets)
    for entry in record.values():
        names.update(entry.secrets)
    return sorted(names)


def _secrets_report(root: Path, config: VectenConfig, record: dict) -> dict:
    from vecten_shared.provider.local import LocalProvider

    declared = declared_secrets(config, record)
    delivered = [n for n in declared if config.has(component_for(n))]
    local = set(LocalProvider(root, inside_compose=False).list_secrets())
    return {
        "undelivered": [n for n in declared if n not in delivered],
        "missing_locally": [n for n in delivered if n not in local],
    }


def load_manifests(config: VectenConfig, candidates=None) -> dict[str, Manifest]:
    """Each listed extension's manifest, read from the environment running the CLI."""
    found: dict[str, Manifest] = {}
    listed = {normalize_name(requirement(r).name) for r in config.extensions}
    # A manifest's entry name carries no meaning, so two extensions may share it; one
    # distribution may still declare only one manifest.
    points = metadata.entry_points(group="vecten.manifest") if candidates is None else candidates
    for point in points:
        dist = getattr(point, "dist", None)
        if dist is None or normalize_name(dist.name) not in listed:
            continue
        name = normalize_name(dist.name)
        if name in found:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {name} declares more than one vecten.manifest entry point",
            )
        try:
            value = point.load()
        except Exception as exc:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {name}: cannot load its manifest ({type(exc).__name__})",
            ) from exc
        found[name] = parse_manifest(name, value)
    return found


def load_services(
    config: VectenConfig, candidates=None, only: set[str] | None = None
) -> dict[str, dict[str, Declaration]]:
    """Each listed extension's `vecten.services` declarations, by extension and service."""
    found: dict[str, dict[str, Declaration]] = {}
    listed = {normalize_name(requirement(r).name) for r in config.extensions}
    if only is not None:
        listed &= only
    points = metadata.entry_points(group=SERVICES_GROUP) if candidates is None else candidates
    for point in points:
        dist = getattr(point, "dist", None)
        if dist is None or normalize_name(dist.name) not in listed:
            continue
        name = normalize_name(dist.name)
        try:
            value = point.load()
        except Exception as exc:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {name}: cannot load service {point.name} ({type(exc).__name__})",
            ) from exc
        found.setdefault(name, {})[point.name] = parse_service(name, point.name, value)
    return found


def installed_from(name: str, path: Path) -> bool:
    """Whether Vecten's own environment holds `name` as an editable install of `path`."""
    try:
        text = metadata.distribution(name).read_text("direct_url.json")
        info = json.loads(text or "{}")
    except metadata.PackageNotFoundError, ValueError:
        return False
    url = info.get("url", "") if isinstance(info, dict) else ""
    if not (info.get("dir_info") or {}).get("editable") or not url.startswith("file://"):
        return False
    return Path(unquote(urlsplit(url).path)).resolve() == path.resolve()


def refuse_stale_services(
    root: Path, config: VectenConfig, record: dict[str, locking.Extension]
) -> None:
    """Refuse when an in-repo extension's code declares services other than its record holds.

    An in-repo extension is installed editable, so its code can change without a new version
    or a changed pyproject.toml, which are what make a lock stale. Only this checkout's own
    editable install shows that code; without one there is nothing to compare.
    """
    local = {
        name
        for name, entry in record.items()
        if entry.source_path and installed_from(name, root / entry.source_path)
    }
    if not local:
        return
    declared = load_services(config, only=local)
    for name in sorted(local):
        current = {s: d.service for s, d in declared.get(name, {}).items()}
        recorded = record[name].services
        changed = sorted(
            s for s in set(current) | set(recorded) if current.get(s) != recorded.get(s)
        )
        if changed:
            raise Failure(
                ExitCode.CONFIG,
                f"extension {name}: service {', '.join(changed)} differs from "
                f"{locking.RECORD_FILE}; run `vecten lock`",
            )


def _unsupported(
    manifests: dict[str, Manifest], services: dict[str, dict[str, Declaration]]
) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for name, manifest in manifests.items():
        found.setdefault(name, []).extend(manifest.unknown)
    for name, declared in services.items():
        for service, declaration in sorted(declared.items()):
            found.setdefault(name, []).extend(
                f"services.{service}.{k}" for k in declaration.unknown
            )
    return {name: keys for name, keys in sorted(found.items()) if keys}


def lock(root: Path, upgrade: tuple[str, ...] = ()) -> dict:
    files, report = lock_project(root, load_config(root), upgrade=upgrade)
    write_project_files(files)
    return report


def _in_repo_package(root: Path, argument: str) -> tuple[PackageMetadata, str]:
    """An in-repo extension's metadata read from its pyproject.toml, and the extras asked for."""
    path, _, extras = argument.partition("[")
    info = in_repo.check(root, path)
    groups = tuple(SimpleNamespace(group=g) for g in info.groups)
    meta = PackageMetadata(info.name, info.version, groups, info.extras, in_repo=True)
    return meta, f"{info.name}[{extras}" if extras else info.name


def _origin(entry: locking.Extension | None) -> dict | None:
    """Where a record entry says an extension comes from, as `extension add` reports it."""
    if entry is None:
        return None
    if entry.git is not None:
        return {
            "kind": "git",
            "version": entry.version,
            **entry.git.as_json(),
            "source_sha256": entry.source_sha256,
        }
    if entry.source_path:
        return {"kind": "path", "version": entry.version, "path": entry.source_path}
    return {"kind": "index", "version": entry.version}


def _git_package(root: Path, package: str, fetched: Path) -> tuple[PackageMetadata, str]:
    """The git source `package` names, resolved, fetched and checked, and the extras asked for."""
    source = git_sources.parse_source(package)
    name = normalize_name(source.name)
    if in_repo.has_folder(root, name):
        raise Failure(
            ExitCode.REFUSED,
            f"the project keeps {name} in extensions/{name}; remove that folder to take {name} "
            "from a git source",
        )
    credentials = git_extensions.credentials(root)
    resolution = git_sources.resolve(source, credentials)
    copy = git_extensions.fetch_copy(
        source, resolution.commit, resolution.tag, resolution.public, credentials, fetched
    )
    git_extensions.check_tag_version(copy)
    groups = tuple(SimpleNamespace(group=g) for g in copy.metadata.groups)
    meta = PackageMetadata(name, copy.metadata.version, groups, copy.metadata.extras, git=copy)
    extras = f"[{','.join(source.extras)}]" if source.extras else ""
    return meta, f"{name}{extras}"


def _refuse_bare_name_of_a_git_source(root: Path, req) -> None:
    name = normalize_name(req.name)
    entry = (locking.read_record(root) or {}).get(name)
    pinned = locking.read_pins(root).get(name, "")
    if req.specifier or not (
        git_extensions.is_source(pinned) or (entry is not None and entry.git is not None)
    ):
        return
    raise Failure(
        ExitCode.USAGE,
        f"{name} comes from a git source; name a tag or a full commit, as in "
        f"`vecten extension add '{name} @ git+https://<host>/<repository>@<tag>'`, or name an "
        f"index version, as in `{name}==<version>`, to take it from the index",
    )


def add_extension(root: Path, package: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="vecten-sources-") as fetched:
        return _add_extension(root, package, Path(fetched))


def _add_extension(root: Path, package: str, fetched: Path) -> dict:
    config = load_config(root)
    if in_repo.is_path(package):
        dist, package = _in_repo_package(root, package)
        req = requirement(package)
    elif git_extensions.is_source(package):
        dist, package = _git_package(root, package, fetched)
        req = requirement(package)
    else:
        req = requirement(package)
        name = normalize_name(req.name)
        if in_repo.has_folder(root, name):
            raise Failure(
                ExitCode.REFUSED,
                f"the project keeps {name} in extensions/{name}; add it with "
                f"`vecten extension add ./extensions/{name}`, so it is never fetched by name",
            )
        _refuse_bare_name_of_a_git_source(root, req)
        dist = inspect_package(str(req), root)
    previous = (locking.read_record(root) or {}).get(normalize_name(dist.name))
    known = sorted({ep.group for ep in dist.entry_points if ep.group in GROUPS})
    unknown = sorted(
        {
            ep.group
            for ep in dist.entry_points
            if ep.group.startswith("vecten.") and ep.group not in GROUPS
        }
    )
    if not set(known) - DECLARATION_GROUPS:
        raise Failure(
            ExitCode.REFUSED,
            f"{req.name} declares no Vecten extension point; an extension declares one or more "
            f"of {', '.join(GROUPS)}, and a manifest alone contributes nothing",
        )
    extras = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
    registration = f"{normalize_name(dist.name)}{extras}"
    # Registration is stable across upgrades; only the dependency records own exact versions.
    config.extensions = [
        registration if normalize_name(requirement(p).name) == normalize_name(req.name) else p
        for p in config.extensions
    ]
    if registration not in config.extensions:
        config.extensions.append(registration)
    files, report = lock_project(root, config, added=dist)
    record = locking.parse_record(files[root / locking.RECORD_FILE]) or {}
    doc = tomlkit.parse((root / "vecten.toml").read_text())
    doc["extensions"] = config.extensions
    name = normalize_name(dist.name)
    services = _bind_services(doc, config, name, record.get(name))
    text = tomlkit.dumps(doc)
    try:
        bound = VectenConfig.model_validate(tomllib.loads(text))
    except ValidationError as exc:
        raise Failure(ExitCode.CONFIG, f"binding the services of {name}: {exc}") from None
    check_bindings(bound, record)
    write_project_files({root / "vecten.toml": text, **files})
    if dist.git is not None:
        pinned = git_extensions.root_line(name, req.extras, dist.git.origin)
    elif dist.in_repo:
        pinned = f"./{in_repo.EXTENSIONS_DIR}/{name}{extras}"
    else:
        pinned = f"{name}{extras}=={dist.version}"
    entry = record.get(name)
    return {
        "package": pinned,
        "groups": known,
        "unknown_groups": unknown,
        "missing_secrets": report["missing_locally"],
        "undelivered": report["undelivered"],
        **services,
        **(
            {"source": entry.git.as_json(), "source_sha256": entry.source_sha256}
            if entry is not None and entry.git is not None
            else {}
        ),
        "previous": _origin(previous),
        "changed": _origin(previous) != _origin(entry),
    }


def _free_label(extension: str, service: str, taken: set[str]) -> str:
    """The service's name when free, else `<extension>-<service>`, numbered when taken too."""
    base = f"{extension}-{service}"[:60].rstrip("-")
    candidates = [service, base, *(f"{base}-{n}" for n in range(2, 100))]
    for label in candidates:
        if label not in taken and label_problem(label) is None:
            return label
    raise Failure(ExitCode.CONFIG, f"no free hostname label for {extension}.{service}")


def _bind_services(doc, config: VectenConfig, extension: str, entry) -> dict:
    """Bind each declared service the project does not bind yet, from its suggestions.

    A bound service keeps its binding as the person left it: an upgrade's new route, or a
    changed suggestion, is only named, so a route never becomes reachable without a diff.
    """
    out: dict[str, list[str]] = {"public_routes": [], "hostnames": [], "binding_notes": []}
    if entry is None or not entry.services:
        return out
    existing = config.services.get(extension, {})
    taken = {b.hostname for t in config.services.values() for b in t.values() if b.hostname}
    for name, service in sorted(entry.services.items()):
        key = f"{extension}.{name}"
        if name in existing:
            for route_name, route in sorted(service.routes.items()):
                bound = existing[name].routes.get(route_name)
                if bound is None:
                    out["binding_notes"].append(f"{key} route {route_name}: declared, not bound")
                elif bound.access != route.access:
                    out["binding_notes"].append(
                        f"{key} route {route_name}: bound {bound.access}, suggested {route.access}"
                    )
            continue
        enabled = [c for c in service.components if config.has(c)]
        if not enabled:
            out["binding_notes"].append(
                f"{key}: runs from {', '.join(service.components)}, which the project does not "
                "enable; not bound"
            )
            continue
        table = tomlkit.table()
        routes = tomlkit.table(is_super_table=True)
        for route_name, route in sorted(service.routes.items()):
            if route.access == "login" and not config.has("mcp"):
                out["binding_notes"].append(
                    f"{key} route {route_name}: login needs the mcp component; not bound"
                )
                continue
            route_table = tomlkit.table()
            route_table["access"] = route.access
            if route.access == "login":
                route_table["groups"] = list(route.groups)
            else:
                out["public_routes"].append(f"{key} route {route_name}")
            routes[route_name] = route_table
        if routes:
            label = _free_label(extension, name, taken)
            taken.add(label)
            table["hostname"] = label
            if config.deploy is not None:
                out["hostnames"].append(service_hostname(label, config.deploy.domain))
        if "platform" not in enabled:
            table["host"] = enabled[0]
        if routes:
            table["routes"] = routes
        if "services" not in doc:
            doc["services"] = tomlkit.table(is_super_table=True)
        if extension not in doc["services"]:
            doc["services"][extension] = tomlkit.table(is_super_table=True)
        doc["services"][extension][name] = table
    return out


def services_message(data: dict) -> list[str]:
    """Lines naming public routes just bound, hostnames needing a deploy, and binding notes."""
    lines = []
    if data.get("public_routes"):
        lines.append("public, reachable by anyone: " + ", ".join(data["public_routes"]))
    if data.get("hostnames"):
        lines.append("new hostnames, served after `vecten deploy`: " + ", ".join(data["hostnames"]))
    lines += data.get("binding_notes", [])
    return lines


def source_changes(data: dict) -> list[str]:
    """Lines naming how `extension add` changed where an extension comes from."""
    previous, source = data.get("previous"), data.get("source")
    if previous is None:
        return []
    if not data.get("changed"):
        return [f"already recorded at {source['commit']}; nothing changed"] if source else []
    if previous["kind"] != "git" or source is None:
        now = "git" if source else ("a path" if data["package"].startswith("./") else "the index")
        was = {"git": "git", "path": "a path", "index": "the index"}[previous["kind"]]
        return [f"switched from {was} (version {previous['version']}) to {now}"]
    lines = [f"previous commit {previous['commit']}, now {source['commit']}"]
    if source["tag"] and source["tag"] == previous["tag"] and source["url"] == previous["url"]:
        lines.append(f"the tag {source['tag']} moved since it was recorded")
    return lines


def secrets_message(missing: list[str], undelivered: list[str]) -> list[str]:
    """Lines naming what to set locally and what no enabled component receives."""
    lines = []
    if missing:
        lines.append(
            "secrets to set: " + ", ".join(f"`vecten secret set {name}`" for name in missing)
        )
    if undelivered:
        lines.append(
            "not delivered, because the project lacks the component that reads them: "
            + ", ".join(undelivered)
        )
    return lines
