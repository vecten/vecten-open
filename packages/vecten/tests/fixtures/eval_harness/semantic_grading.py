"""Read the artifacts a semantic evaluation produced, independently of the agent's transcript."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlglot import parse_one

from eval_harness.gh_standin import Store
from eval_harness.projects import BUILDERS, REPOSITORY, semantic_fixture
from vecten_shared.osi.evals import EVALS_PATH, load_cases, run_case
from vecten_shared.osi.issues import ModelError
from vecten_shared.osi.semantic import SemanticModel


def _git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


def evidence(root, scenario, github=None):
    fixture = semantic_fixture(BUILDERS[scenario].grader)
    report = {"reference_expression": fixture["fixed_expression"], "matching_cases": []}
    store = Store(github) if github else None
    pulls = (store.read(REPOSITORY) or {}).get("pulls", {}) if store else {}
    candidates = [p for p in pulls.values() if p["base"] == "main" and p["state"] == "open"]
    if len(candidates) > 1:
        return {**report, "error": "multiple open pull requests target main"}
    repo, head, main = root, "HEAD", "origin/main"
    if candidates:
        pull = candidates[0]
        repo, main = store.bare(REPOSITORY), "refs/heads/main"
        # The branch may receive further fixes after the stand-in records the original head.
        head = (
            _git(repo, "rev-parse", "--verify", f"refs/heads/{pull['head']}^{{commit}}")
            .decode()
            .strip()
        )
        report["pull_head"] = head
    if scenario == "semantic-1":
        baseline = (
            _git(repo, "log", "-1", "--format=%H", "--grep=^Record the first deploy$", main)
            .decode()
            .strip()
        )
        if not baseline:
            return {**report, "error": "could not find the seeded deploy commit"}
        paths = ["--", "dbt", "osi/datasets.json"]
        diff = _git(repo, "diff", "--name-only", baseline, head, *paths)
        dirty = _git(root, "diff", "--name-only", "HEAD", *paths)
        additions = _git(root, "ls-files", "--others", "--exclude-standard", *paths)
        report["changed_datasets_or_dbt"] = sorted(
            set((diff + dirty + additions).decode().splitlines())
        )
    if not candidates:
        return _model_evidence(root, fixture, report)
    with TemporaryDirectory() as temp:
        snapshot = Path(temp)
        entries = _git(repo, "ls-tree", "-rz", head, "--", "osi", str(EVALS_PATH))
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            info, path = entry.split(b"\t", 1)
            mode, kind, oid = info.split()
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                return {**report, "error": "model and eval files must be regular files"}
            target = snapshot / path.decode()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_git(repo, "cat-file", "blob", oid.decode()))
        return _model_evidence(snapshot, fixture, report)


def _model_evidence(root, fixture, report):
    try:
        model = SemanticModel.load(root / "osi", kind="postgres")
        cases = load_cases(root / EVALS_PATH)
    except (ModelError, OSError, ValueError) as exc:
        return {**report, "error": str(exc)}
    for case in cases:
        if case.expect["outcome"] != "compiled" or not run_case(model, case).ok:
            continue
        for name in case.request.get("metrics", []):
            metric = model.metrics.get(name)
            if (
                not metric
                or not case.question
                or case.question not in metric.get("ai_context", {}).get("examples", [])
            ):
                continue
            if "gap" in fixture and (
                case.gap != fixture["gap"]["id"]
                or case.question != fixture["question"]
                or name not in fixture["gap"]["names"]
            ):
                continue
            expr = metric["expression_sql"]
            same = parse_one(expr, read="postgres") == parse_one(
                fixture["fixed_expression"], read="postgres"
            )
            report["matching_cases"].append(
                {
                    "name": case.name,
                    "metric": name,
                    "expression": expr,
                    "matches_reference_expression": same,
                }
            )
    return report
