"""Every document under osi/, merged by name: dbt scans the same tree the MCP loads."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from vecten_shared.osi import OSI_VERSION
from vecten_shared.osi.issues import Issue, ModelError


@dataclass(frozen=True)
class Document:
    path: Path
    data: dict


@dataclass
class Model:
    datasets: dict[str, tuple[dict, Path]] = field(default_factory=dict)
    metrics: dict[str, tuple[dict, Path]] = field(default_factory=dict)
    relationships: dict[str, tuple[dict, Path]] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)


def document_sources(osi_dir: Path) -> list[tuple[str, Path]]:
    from vecten_shared.config import load_config
    from vecten_shared.extensions import extension_paths

    directories = [("project", osi_dir)]
    if (osi_dir.parent / "vecten.toml").is_file():
        directories += [
            (f"extension:{name}", path)
            for name, path in extension_paths(load_config(osi_dir.parent), "vecten.osi").items()
        ]
    found = []
    for namespace, directory in sorted(directories):
        if directory.is_dir():
            found.extend(
                sorted(
                    (f"{namespace}:{p.relative_to(directory).as_posix()}", p)
                    for p in directory.rglob("*")
                    if p.is_file()
                    and not any(part.startswith(".") for part in p.relative_to(directory).parts)
                )
            )
    return found


def list_documents(osi_dir: Path) -> list[Path]:
    return [path for _, path in document_sources(osi_dir)]


def load_document(path: Path) -> Document:
    if path.suffix.lower() != ".json":
        raise ModelError(
            [
                Issue(
                    "not_json_document",
                    "Ossie documents under osi/ are JSON; dbt and the MCP ignore every other file",
                    str(path),
                )
            ]
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ModelError([Issue("invalid_json", str(exc), str(path))]) from None
    if not isinstance(data, dict):
        raise ModelError([Issue("invalid_document", "the root must be a JSON object", str(path))])
    return Document(path, data)


def _elements(doc: Document, key: str) -> list[dict]:
    return [
        e for sm in doc.data["semantic_model"] for e in (sm.get(key) or []) if isinstance(e, dict)
    ]


def merge(documents: list[Document]) -> Model:
    issues: list[Issue] = []
    model = Model()
    for doc in documents:
        where = doc.path.as_posix()
        version = doc.data.get("version")
        if version != OSI_VERSION:
            issues.append(
                Issue(
                    "unsupported_version",
                    f"version must be {OSI_VERSION!r}; got {version!r}",
                    where,
                )
            )
            continue
        semantic_models = doc.data.get("semantic_model")
        if not isinstance(semantic_models, list) or not all(
            isinstance(sm, dict) for sm in semantic_models
        ):
            issues.append(
                Issue("invalid_document", "semantic_model must be a list of objects", where)
            )
            continue
        model.names += [str(sm.get("name")) for sm in semantic_models]
        for key, target in (
            ("datasets", model.datasets),
            ("metrics", model.metrics),
            ("relationships", model.relationships),
        ):
            kind = key[:-1]
            for element in _elements(doc, key):
                name = element.get("name")
                if not isinstance(name, str):
                    issues.append(Issue("invalid_document", f"a {kind} has no name", where))
                    continue
                if name in target:
                    first = target[name][1].as_posix()
                    issues.append(
                        Issue(
                            "duplicate_name",
                            f"{kind} {name!r} is defined in {first} and {where}",
                            where,
                        )
                    )
                    continue
                target[name] = (element, doc.path)
    if issues:
        raise ModelError(issues)
    return model
