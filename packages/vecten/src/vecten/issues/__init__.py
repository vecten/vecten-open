"""`vecten issues`: what is broken in a running platform, from every registered source, in the
shared issue shape. Reading changes nothing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vecten.issues.airflow import collect_airflow
from vecten.issues.context import IssueContext, SourceUnavailable
from vecten.issues.dbt import collect_dbt
from vecten.issues.gaps import collect_gaps
from vecten.issues.mcp_health import collect_mcp_health
from vecten_shared.config import VectenConfig
from vecten_shared.issues import Issue

__all__ = ["ADAPTERS", "Adapter", "Collected", "SourceUnavailable", "collect", "find"]


@dataclass(frozen=True)
class Adapter:
    name: str
    """The `source` of its issues."""
    id_prefix: str
    """What every id it derives starts with, which is how `find` picks the one source to read."""
    applies: Callable[[VectenConfig], bool]
    collect: Callable[[IssueContext], list[Issue]]
    lookup: Callable[[IssueContext], list[Issue]] | None = None
    """The issues `find` searches, when it resolves more ids than the list shows."""


@dataclass(frozen=True)
class Collected:
    issues: list[Issue]
    sources: dict[str, str]

    @property
    def complete(self) -> bool:
        return all(state == "ok" for state in self.sources.values())


ADAPTERS: dict[str, Adapter] = {}


def register_adapter(adapter: Adapter) -> None:
    ADAPTERS[adapter.name] = adapter


register_adapter(
    Adapter(
        "airflow",
        "iss_airflow_",
        lambda c: c.has("platform"),
        collect_airflow,
        lambda ctx: collect_airflow(ctx, summaries=True),
    )
)
register_adapter(Adapter("dbt", "iss_dbt_", lambda c: c.has("platform"), collect_dbt))
register_adapter(Adapter("gaps", "iss_gap_", lambda c: c.has("mcp"), collect_gaps))
register_adapter(Adapter("mcp", "iss_mcp_", lambda c: c.has("mcp"), collect_mcp_health))


def _read(ctx: IssueContext, adapters: list[Adapter], *, lookup: bool = False) -> Collected:
    issues: list[Issue] = []
    sources: dict[str, str] = {}
    for adapter in adapters:
        if not adapter.applies(ctx.config):
            continue
        read = (adapter.lookup if lookup else None) or adapter.collect
        try:
            issues += read(ctx)
        except SourceUnavailable as exc:
            sources[adapter.name] = f"unavailable: {exc}"
            continue
        sources[adapter.name] = "ok"
    return Collected(issues, sources)


def collect(ctx: IssueContext) -> Collected:
    return _read(ctx, list(ADAPTERS.values()))


def find(ctx: IssueContext, issue_id: str) -> Issue | None:
    """The open issue `issue_id`, read from the one source its id names."""
    adapter = next((a for a in ADAPTERS.values() if issue_id.startswith(a.id_prefix)), None)
    if adapter is None:
        return None
    collected = _read(ctx, [adapter], lookup=True)
    if not collected.complete:
        state = collected.sources.get(adapter.name, f"{adapter.name} cannot be read")
        raise SourceUnavailable(state)
    return next((i for i in collected.issues if i.id == issue_id), None)
