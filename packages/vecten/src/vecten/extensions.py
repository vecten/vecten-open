"""Install extensions and record portable, credential-free project dependencies."""

import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import tomlkit

from vecten import locking
from vecten.stack.images import BASE_CONSTRAINTS_SCRIPT, base_image
from vecten_shared.config import VectenConfig, load_config
from vecten_shared.extensions import GROUPS, requirement
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_text_atomic
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


def _uv(args: list[str], root: Path) -> None:
    if not shutil.which("uv"):
        raise Failure(ExitCode.PREREQUISITE, "install uv to manage extension dependencies")
    env = package_environment(root)
    try:
        result = subprocess.run(
            ["uv", "--no-config", *args],
            env=env,
            capture_output=True,
            text=True,
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


def install_lock(lock: str, root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="vecten-install-") as temp:
        path = Path(temp) / "requirements.lock"
        path.write_text(lock)
        constraints = release_constraints(Path(temp) / "constraints.txt")
        _uv(
            [
                "pip",
                "install",
                "--python",
                sys.executable,
                "--constraint",
                str(constraints),
                "-r",
                str(path),
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
) -> str:
    """Resolve `requirements`, keeping every pin of `previous` that still satisfies them."""
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
        )
        text = lock.read_text()
        from packaging.requirements import InvalidRequirement, Requirement

        try:
            for line in text.splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                req = Requirement(line)
                if (
                    req.url
                    or not all(s.operator == "==" and "*" not in s.version for s in req.specifier)
                    or not req.specifier
                ):
                    raise InvalidRequirement()
        except InvalidRequirement:
            raise Failure(
                ExitCode.REFUSED, "package resolution produced a non-portable dependency lock"
            ) from None
        return text


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
    root: Path, config: VectenConfig, added: PackageMetadata | None
) -> tuple[dict[str, str], dict[str, locking.Extension]]:
    """Each registered extension's exact pin and record entry, inspecting only what is unknown.

    `requirements.txt` may still hold an earlier release's connector dependencies; only the
    registered extensions' lines are kept.
    """
    pins = locking.read_pins(root)
    record = locking.read_record(root) or {}
    found_pins, found_record = {}, {}
    for registration in config.extensions:
        req = requirement(registration)
        name = normalize_name(req.name)
        extras = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
        if added is not None and normalize_name(added.name) == name:
            dist = added
        else:
            pinned, entry = pins.get(name), record.get(name)
            if pinned and entry and _pinned_version(pinned) == entry.version:
                found_pins[name], found_record[name] = f"{name}{extras}=={entry.version}", entry
                continue
            dist = inspect_package(pinned or registration, root)
        found_pins[name] = f"{name}{extras}=={dist.version}"
        found_record[name] = locking.Extension(version=dist.version, extras=tuple(dist.extras))
    return found_pins, found_record


def lock_project(
    root: Path,
    config: VectenConfig,
    *,
    upgrade: tuple[str, ...] = (),
    added: PackageMetadata | None = None,
) -> tuple[dict[Path, str], dict]:
    """Resolve every component's lock and install the CLI's; return the records to write.

    Nothing is written here: the caller writes these files together, so a failure in any
    resolution leaves every committed record as it was.
    """
    pins, record = _extension_pins(root, config, added)
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
            )
        except Failure as failure:
            raise Failure(failure.code, f"{name}: {failure.message}") from None
        digest = locking.inputs_hash(component, lock_roots)
        body = "".join(f"{line}\n" for line in text.splitlines() if not line.startswith("#"))
        locks[component] = f"{locking.stamp_line(component, digest)}\n{body}"
    install_lock(locks["cli"], root)
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
    }
    return files, report


def lock(root: Path, upgrade: tuple[str, ...] = ()) -> dict:
    files, report = lock_project(root, load_config(root), upgrade=upgrade)
    write_project_files(files)
    return report


def add_extension(root: Path, package: str) -> dict:
    req = requirement(package)
    config = load_config(root)
    dist = inspect_package(str(req), root)
    known = sorted({ep.group for ep in dist.entry_points if ep.group in GROUPS})
    unknown = sorted(
        {
            ep.group
            for ep in dist.entry_points
            if ep.group.startswith("vecten.") and ep.group not in GROUPS
        }
    )
    if not known:
        raise Failure(ExitCode.REFUSED, f"{req.name} declares no Vecten extension point")
    extras = f"[{','.join(sorted(req.extras))}]" if req.extras else ""
    registration = f"{normalize_name(dist.name)}{extras}"
    # Registration is stable across upgrades; only the dependency records own exact versions.
    config.extensions = [
        registration if normalize_name(requirement(p).name) == normalize_name(req.name) else p
        for p in config.extensions
    ]
    if registration not in config.extensions:
        config.extensions.append(registration)
    files, _ = lock_project(root, config, added=dist)
    doc = tomlkit.parse((root / "vecten.toml").read_text())
    doc["extensions"] = config.extensions
    write_project_files({root / "vecten.toml": tomlkit.dumps(doc), **files})
    pinned = f"{normalize_name(dist.name)}{extras}=={dist.version}"
    return {"package": pinned, "groups": known, "unknown_groups": unknown}
