"""`extensions.lock.json`: what each locked extension declares, read without importing it.

`vecten lock` writes it; the CLI, the image build and both runtimes read it, so a project's
CI and hosts know what an extension needs without installing the extension.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vecten_shared.apps import App, app_from_json, app_to_json
from vecten_shared.services import Service, name_problem, service_from_json, service_to_json

RECORD_FILE = "extensions.lock.json"
SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class GitOrigin:
    """Where a git-sourced extension comes from: the commit `requirements.txt` records."""

    # https://<host>/<repository>, never with user information.
    url: str
    commit: str
    subdirectory: str | None
    # The tag that pointed at `commit` when it was recorded, or None when a commit was named.
    tag: str | None
    # Whether the host answered without a credential when `commit` was recorded.
    public: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "commit": self.commit,
            "subdirectory": self.subdirectory,
            "tag": self.tag,
            "public": self.public,
        }


@dataclass(frozen=True)
class Extension:
    version: str
    extras: tuple[str, ...]
    secrets: tuple[str, ...] = ()
    # The manifest's settings schema, or None when the extension declares no settings.
    settings: dict[str, Any] | None = field(default=None, hash=False)
    # An in-repo extension's project-relative path, its pyproject.toml's sha256, and its build
    # requirements pinned exactly; None and empty for an extension from an index.
    source_path: str | None = None
    metadata_sha256: str | None = None
    build_requires: tuple[str, ...] = ()
    # A git-sourced extension's origin and the hash of its source at that commit, which every
    # fetch and the image build check; None for any other extension.
    git: GitOrigin | None = None
    source_sha256: str | None = None
    # The `vecten.services` declarations, by service name.
    services: dict[str, Service] = field(default_factory=dict, hash=False)
    # The `vecten.apps` declarations, by app name.
    apps: dict[str, App] = field(default_factory=dict, hash=False)


def record_text(record: dict[str, Extension]) -> str:
    body = {
        "version": 1,
        "extensions": {
            name: {
                "version": entry.version,
                "extras": sorted(entry.extras),
                "secrets": sorted(entry.secrets),
                "settings": entry.settings,
                **(
                    {
                        "source": {"path": entry.source_path},
                        "metadata_sha256": entry.metadata_sha256,
                        "build_requires": list(entry.build_requires),
                    }
                    if entry.source_path
                    else {}
                ),
                **(
                    {
                        "source": entry.git.as_json(),
                        "source_sha256": entry.source_sha256,
                        "build_requires": list(entry.build_requires),
                    }
                    if entry.git
                    else {}
                ),
                **(
                    {
                        "services": {
                            service: service_to_json(declared)
                            for service, declared in sorted(entry.services.items())
                        }
                    }
                    if entry.services
                    else {}
                ),
                **(
                    {"apps": {app: app_to_json(d) for app, d in sorted(entry.apps.items())}}
                    if entry.apps
                    else {}
                ),
            }
            for name, entry in sorted(record.items())
        },
    }
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def _git_origin(name: str, source: dict, digest: object) -> GitOrigin | None:
    """The origin `source` records, or None when any field breaks a git source's rules."""
    from vecten_shared.git_sources import FORBIDDEN_IN_REF, FULL_COMMIT, parse_source
    from vecten_shared.output import Failure

    origin = GitOrigin(
        url=source["url"],
        commit=source["commit"],
        subdirectory=source.get("subdirectory"),
        tag=source.get("tag"),
        public=source["public"],
    )
    if (
        set(source) - {"url", "commit", "subdirectory", "tag", "public"}
        or not all(isinstance(v, str) for v in (origin.url, origin.commit))
        or not FULL_COMMIT.fullmatch(origin.commit)
        or not (origin.subdirectory is None or isinstance(origin.subdirectory, str))
        or not (origin.tag is None or isinstance(origin.tag, str))
        or (origin.tag is not None and FORBIDDEN_IN_REF.search(origin.tag))
        or not isinstance(origin.public, bool)
        or not (isinstance(digest, str) and SHA256.fullmatch(digest))
    ):
        return None
    fragment = f"#subdirectory={origin.subdirectory}" if origin.subdirectory else ""
    try:
        parsed = parse_source(f"{name} @ git+{origin.url}@{origin.commit}{fragment}")
    except Failure:
        return None
    if parsed.url != origin.url or parsed.subdirectory != origin.subdirectory:
        return None
    return origin


def parse_record(text: str) -> dict[str, Extension] | None:
    """The record, or None when any part is malformed: a partial record is never trusted."""
    try:
        entries = json.loads(text)["extensions"].items()
        record = {}
        for name, entry in entries:
            version, extras = entry["version"], entry["extras"]
            secrets, settings = entry.get("secrets", []), entry.get("settings")
            source = entry.get("source") or {}
            path, digest = source.get("path"), entry.get("metadata_sha256")
            build = entry.get("build_requires", [])
            git = None
            if "url" in source:
                git = _git_origin(name, source, entry.get("source_sha256"))
                if git is None:
                    return None
            services, apps = entry.get("services", {}), entry.get("apps", {})
            if not isinstance(services, dict) or any(
                name_problem("service", service) for service in services
            ):
                return None
            if not isinstance(apps, dict):
                return None
            if (
                not isinstance(version, str)
                or not isinstance(secrets, list)
                or not all(isinstance(e, str) for e in extras)
                or not all(isinstance(s, str) for s in secrets)
                or not (settings is None or isinstance(settings, dict))
                # An in-repo extension lives in its own folder, never elsewhere in or outside
                # the project.
                or not (path is None or (path == f"extensions/{name}" and isinstance(digest, str)))
                or (path is not None and git is not None)
                or not all(isinstance(b, str) for b in build)
            ):
                return None
            record[name] = Extension(
                version=version,
                extras=tuple(sorted(extras)),
                secrets=tuple(sorted(secrets)),
                settings=settings,
                source_path=path,
                metadata_sha256=digest,
                build_requires=tuple(build),
                git=git,
                source_sha256=entry.get("source_sha256") if git else None,
                services={service: service_from_json(value) for service, value in services.items()},
                apps={app: app_from_json(app, value) for app, value in apps.items()},
            )
        return record
    except ValueError, KeyError, TypeError, AttributeError:
        return None


def read_record(root: Path) -> dict[str, Extension] | None:
    path = root / RECORD_FILE
    if not path.is_file():
        return None
    return parse_record(path.read_text(encoding="utf-8"))
