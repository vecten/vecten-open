"""Validate shipped models and retain the last usable snapshot in the state database."""

import copy
import json
import threading
import time
from dataclasses import asdict

from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope

from vecten_shared.osi import OSI_VERSION
from vecten_shared.osi.issues import ModelError
from vecten_shared.osi.revision import build_covers, content_revision
from vecten_shared.osi.semantic import SemanticModel
from vecten_shared.osi.sql_policy import source_sql


def source_tables(source, kind):
    tree = parse_one("SELECT * FROM " + source_sql(source, kind), read=kind)
    tables = {}
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                table = source.copy()
                table.set("alias", None)
                tables[table.sql(dialect=kind)] = table
    return list(tables.values())


def search_index(model):
    entries = []
    for name, dataset in model.datasets.items():
        entries.append(
            {
                "kind": "dataset",
                "name": name,
                "description": dataset.get("description", ""),
                "ai_context": dataset.get("ai_context", {}),
            }
        )
        entries.extend(
            {"kind": "field", "dataset": name, **copy.deepcopy(field)}
            for field in dataset.get("fields", [])
        )
    entries.extend(
        {"kind": "metric", "dataset": model.homes[name], **copy.deepcopy(metric)}
        for name, metric in model.metrics.items()
    )
    return entries


# An unchanged model's sources are checked again at most this often: on Snowflake each check
# resumes the compute warehouse, which bills until it suspends.
SOURCE_RECHECK = 3600


class ModelService:
    def __init__(
        self,
        directory,
        state,
        *,
        kind,
        source_exists,
        platform=False,
        commit=lambda: None,
        built=lambda: None,
        on_swap=None,
        clock=time.time,
    ):
        self.directory, self.state, self.kind = directory, state, kind
        self.source_exists, self.platform = source_exists, platform
        self.commit, self.built, self.on_swap, self.clock = commit, built, on_swap, clock
        self._lock = threading.RLock()
        self._pending_hook = False
        # (revision, commit, when) of the last source check that found every source.
        self._sources_found = None
        saved = state.get("semantic_model", "live")
        self.current = SemanticModel(**saved["model"]) if saved else None
        self._health = {
            "status": "not loaded",
            "spec_version": OSI_VERSION,
            "errors": [],
            "warnings": [],
            "candidate_commit": None,
        }

    def health(self):
        with self._lock:
            return copy.deepcopy(
                {**self._health, "live_commit": self.current.commit if self.current else None}
            )

    def _hook(self):
        if not self._pending_hook or not self.on_swap:
            return
        try:
            self.on_swap(self.current)
        except Exception:
            self._health["warnings"].append(
                {"code": "swap_hook_pending", "message": "model change reconciliation will retry"}
            )
        else:
            self._pending_hook = False

    def reload(self):
        with self._lock:
            health = {
                "status": "error",
                "spec_version": OSI_VERSION,
                "errors": [],
                "warnings": [],
                "checked_at": self.clock(),
                "candidate_commit": None,
            }
            self._health = health
            try:
                revision = content_revision(self.directory)
                commit = self.commit() or revision
                health["candidate_commit"] = commit
                candidate = SemanticModel.load(self.directory, kind=self.kind, commit=commit)
                health["warnings"] = candidate.report.get("warnings", [])
                if content_revision(self.directory) != revision:
                    health["errors"] = [
                        {
                            "code": "bundle_changed",
                            "message": "bundle changed during validation; retrying",
                        }
                    ]
                    return False
                if self.platform and not build_covers(self.built(), commit):
                    health["status"] = "shipped, waiting for build"
                    return False
                found = self._sources_found
                fresh = (
                    found is not None
                    and found[:2] == (revision, commit)
                    and self.clock() - found[2] < SOURCE_RECHECK
                )
                for name, dataset in () if fresh else candidate.datasets.items():
                    for table in source_tables(dataset["source"], self.kind):
                        try:
                            exists = self.source_exists(table)
                        except Exception:
                            health["errors"].append(
                                {
                                    "code": "source_check_unavailable",
                                    "dataset": name,
                                    "message": "warehouse metadata unavailable",
                                }
                            )
                            break
                        if not exists:
                            health["errors"].append(
                                {
                                    "code": "source_missing",
                                    "dataset": name,
                                    "source": table.sql(dialect=self.kind),
                                }
                            )
                if health["errors"]:
                    return False
                if not fresh:
                    self._sources_found = (revision, commit, self.clock())
                health["status"] = "ready"
                saved = self.state.get("semantic_model", "live")
                changed = (
                    not saved or saved["revision"] != revision or saved["model"]["commit"] != commit
                )
                if changed:
                    self.state.put(
                        "semantic_model",
                        "live",
                        {
                            "model": asdict(candidate),
                            "revision": revision,
                            "index": search_index(candidate),
                        },
                    )
                    self.current = candidate
                    self._pending_hook = True
                self._hook()
                return changed
            except ModelError as exc:
                health["errors"] = [asdict(issue) for issue in exc.issues]
            except Exception:
                health["errors"] = [
                    {
                        "code": "model_load_unavailable",
                        "message": "model or build evidence unavailable",
                    }
                ]
            return False

    def search(self, query, *, denied=()):
        with self._lock:
            if self.current is None:
                raise ValueError("semantic model is not ready")
            words = str(query).casefold().split()
            if not words:
                return []
            saved = self.state.get("semantic_model", "live")
            denied = {name.casefold() for name in denied}
            visible_metrics = self.current.visible_metrics(denied)
            results = []
            for entry in saved["index"]:
                if (
                    entry["kind"] == "field"
                    and (entry["dataset"] + "." + entry["name"]).casefold() in denied
                ):
                    continue
                if entry["kind"] == "metric" and entry["name"] not in visible_metrics:
                    continue
                text = json.dumps(
                    {key: entry.get(key) for key in ("name", "description", "ai_context")}
                ).casefold()
                if all(word in text for word in words):
                    results.append(entry)
            return copy.deepcopy(results[:100])
