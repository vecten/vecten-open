"""The semantic-model items `next` reports, from `osi/`, `.env` and run records only."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vecten.next.items import Item
from vecten.next.modelling import BUILD_COMMAND, BUILD_RECORD, sha256_of
from vecten_shared.config import VectenConfig
from vecten_shared.osi.documents import document_sources, load_document
from vecten_shared.osi.issues import ModelError
from vecten_shared.provider.local import LocalProvider
from vecten_shared.records import read_record

VALIDATE_RECORD = "mcp-validate"
ADMIN_SECRET = "WAREHOUSE_ADMIN_PASSWORD"


def osi_digest(osi_dir: Path) -> str:
    """sha256 over every non-hidden file under `osi_dir`, in sorted relative-path order.

    Each file contributes its relative path, a NUL byte, its bytes and a newline, so a rename
    changes the digest as much as an edit does. A missing directory digests the empty input.
    """
    digest = hashlib.sha256()
    for name, path in document_sources(osi_dir):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\n")
    return digest.hexdigest()


def _count(path: Path, key: str) -> int:
    """How many `key` elements the document holds across its semantic models, unvalidated.

    A document that does not parse counts as empty: the validate item reports the real problem.
    """
    if not path.is_file():
        return 0
    try:
        models = load_document(path).data.get("semantic_model")
    except ModelError:
        return 0
    if not isinstance(models, list):
        return 0
    return sum(
        len(sm[key]) for sm in models if isinstance(sm, dict) and isinstance(sm.get(key), list)
    )


def _unrefined(path: Path) -> int:
    """How many datasets lack a non-blank description or a non-empty `primary_key` list.

    A document that does not parse counts as 0, as in `_count`: the validate item reports it.
    """
    if not path.is_file():
        return 0
    try:
        models = load_document(path).data.get("semantic_model")
    except ModelError:
        return 0
    if not isinstance(models, list):
        return 0
    datasets = [
        d
        for sm in models
        if isinstance(sm, dict) and isinstance(sm.get("datasets"), list)
        for d in sm["datasets"]
        if isinstance(d, dict)
    ]
    return sum(
        1
        for d in datasets
        if not (isinstance(d.get("description"), str) and d["description"].strip())
        or not (isinstance(d.get("primary_key"), list) and d["primary_key"])
    )


def _model_items(root: Path, *, metrics_wanted: bool = True) -> list[Item]:
    osi_dir = root / "osi"
    items: list[Item] = []
    if metrics_wanted and not _count(osi_dir / "metrics.json", "metrics"):
        items.append(Item("skill", "No metrics defined in osi/metrics.json", "semantic"))
    record = read_record(root, VALIDATE_RECORD)
    if not record or not record.get("ok") or record.get("osi_sha256") != osi_digest(osi_dir):
        items.append(Item("skill", "Validate the semantic model", "semantic"))
    return items


def _platform_items(root: Path) -> list[Item]:
    record = read_record(root, BUILD_RECORD)
    # Without a green build the modelling build item already asks for the build that writes it.
    if record and record.get("ok"):
        if record.get("datasets_sha256") != sha256_of(root / "osi" / "datasets.json"):
            title = "osi/datasets.json is stale against the last local build"
            return [Item("command", title, BUILD_COMMAND)]
    return []


def _mcp_only_items(root: Path, config: VectenConfig) -> list[Item]:
    items: list[Item] = []
    # Snowflake introspection signs in with a key pair it generates, so only Postgres asks.
    stored = LocalProvider(root, inside_compose=False).list_secrets()
    if config.warehouse.kind == "postgres" and ADMIN_SECRET not in stored:
        title = "Store the warehouse admin credential"
        items.append(Item("command", title, f"vecten secret set {ADMIN_SECRET}"))
    refine = Item("skill", "Refine the drafted datasets", "semantic")
    if not _count(root / "osi" / "datasets.json", "datasets"):
        title = "Draft osi/datasets.json from the warehouse"
        items.append(Item("command", title, "vecten mcp introspect"))
        items.append(refine)
    elif _unrefined(root / "osi" / "datasets.json"):
        items.append(refine)
    return items


def semantic_items(root: Path, config: VectenConfig) -> list[Item]:
    if config.has("platform"):
        # A platform started empty has no dataset yet, so no metric could measure anything.
        wanted = bool(_count(root / "osi" / "datasets.json", "datasets"))
        return _platform_items(root) + _model_items(root, metrics_wanted=wanted)
    return _mcp_only_items(root, config) + _model_items(root)
