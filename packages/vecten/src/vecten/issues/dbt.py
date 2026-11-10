"""dbt test, model and source freshness failures from the results the dbt DAG archives per run.

The runs come from Airflow's `dbt_build` task instances, newest first; a node is open while
the newest run that reports it failed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass

from vecten.issues.context import IssueContext, SourceUnavailable, iso
from vecten.issues.resolutions import DBT_DAG
from vecten_platform.dbt.artifacts import run_keys
from vecten_shared.issues import Issue, cap, dbt_issue_id, freshness_issue_id, redact, untrusted

RUNS = 10
MESSAGE_LIMIT = 1000
FAILED = {"fail", "error", "runtime error"}
# Freshness reports a source past its warn threshold as `warn`, and the DAG treats it as stale.
STALE = {"warn", "error", "runtime error"}
# Only a node that ran and passed clears a failure; a skipped node never ran, since dbt skips
# whatever depends on a failed node.
PASSED = {"pass", "success"}
STEPS = ("build", "convention", "sources")


@dataclass(frozen=True)
class NodeResult:
    run_id: str
    step: str
    location: str
    generated_at: str | None
    result: dict


def _runs(ctx: IssueContext) -> list[str]:
    newest: dict[str, object] = {}
    for attempt in ctx.attempts():
        if attempt.dag_id == DBT_DAG:
            current = newest.get(attempt.run_id)
            if current is None or attempt.run_after > current:
                newest[attempt.run_id] = attempt.run_after
    return sorted(newest, key=lambda run: newest[run], reverse=True)[:RUNS]


def _read(ctx: IssueContext, key: str) -> dict | None:
    body = ctx.artifacts.read(key) if ctx.artifacts is not None else None
    if body is None:
        return None
    try:
        doc = json.loads(body)
    except ValueError:
        raise SourceUnavailable(f"{ctx.artifacts.location(key)} is not valid JSON") from None
    return doc if isinstance(doc, dict) else None


def _results(ctx: IssueContext) -> dict[str, list[NodeResult]]:
    """Every node's results, newest run first."""
    nodes: dict[str, list[NodeResult]] = defaultdict(list)
    for run_id in _runs(ctx):
        keys = run_keys(run_id)
        for step in STEPS:
            doc = _read(ctx, keys[step])
            if doc is None:
                continue
            stamp = (doc.get("metadata") or {}).get("generated_at")
            location = ctx.artifacts.location(keys[step])
            for result in doc.get("results") or []:
                if isinstance(result, dict) and result.get("unique_id"):
                    nodes[result["unique_id"]].append(
                        NodeResult(run_id, step, location, stamp, result)
                    )
    return nodes


def _name(unique_id: str) -> str:
    """`test.<package>.<name>.<hash>` and `model.<package>.<name>` name the node third;
    a source is `source.<package>.<source>.<table>`."""
    parts = unique_id.split(".")
    if parts[0] == "source":
        return ".".join(parts[2:])
    return parts[2] if len(parts) > 2 else unique_id


def node_issue_id(unique_id: str) -> str:
    name = _name(unique_id)
    return freshness_issue_id(name) if unique_id.startswith("source.") else dbt_issue_id(name)


def collect_dbt(ctx: IssueContext) -> list[Issue]:
    issues = []
    for unique_id, results in sorted(_results(ctx).items()):
        source = unique_id.startswith("source.")
        bad = STALE if source else FAILED
        failing = []
        for node in results:
            status = node.result.get("status")
            if status in PASSED:
                break
            if status in bad:
                failing.append(node)
        if not failing:
            continue
        newest = failing[0]
        status = str(newest.result.get("status"))
        name = _name(unique_id)
        evidence = {
            "run_id": newest.run_id,
            "node": unique_id,
            "status": status,
            "failures": newest.result.get("failures"),
            "message": cap(
                redact(str(newest.result.get("message") or ""), ctx.secrets), MESSAGE_LIMIT
            ),
            "location": newest.location,
        }
        if source:
            connector = name.split(".", 1)[0].removeprefix("raw_")
            title = f"Source {name} is stale ({status}); the {connector} sync is not landing it"
            resolution = "connector"
        else:
            kind = unique_id.split(".", 1)[0]
            title = f"dbt {kind} {name} fails ({status})"
            resolution = "model"
        issues.append(
            Issue(
                id=node_issue_id(unique_id),
                source="dbt",
                kind="skill",
                title=title,
                resolution=resolution,
                occurrences=len(failing),
                first_seen=iso(failing[-1].generated_at),
                last_seen=iso(newest.generated_at),
                evidence=untrusted(evidence),
            )
        )
    return issues
