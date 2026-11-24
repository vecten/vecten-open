"""Read the artifacts a semantic evaluation produced, independently of the agent's transcript."""

import subprocess

from sqlglot import parse_one

from eval_harness.projects import BUILDERS, semantic_fixture
from vecten_shared.osi.evals import EVALS_PATH, load_cases, run_case
from vecten_shared.osi.issues import ModelError
from vecten_shared.osi.semantic import SemanticModel


def evidence(root, scenario):
    fixture = semantic_fixture(BUILDERS[scenario].grader)
    report = {"reference_expression": fixture["fixed_expression"], "matching_cases": []}
    if scenario == "semantic-1":
        baseline = subprocess.check_output(
            ["git", "log", "-1", "--format=%H", "--grep=^Record the first deploy$", "origin/main"],
            cwd=root,
            text=True,
        ).strip()
        if not baseline:
            return {**report, "error": "could not find the seeded deploy commit"}
        tracked = subprocess.check_output(
            ["git", "diff", "--name-only", baseline, "--", "dbt", "osi/datasets.json"], cwd=root
        )
        additions = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "dbt", "osi/datasets.json"],
            cwd=root,
        )
        report["changed_datasets_or_dbt"] = (tracked + additions).decode().splitlines()
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
            value = case.expect.get("value", {})
            if "expected_by_country" in fixture:
                where = value.get("where", {})
                expected = (
                    fixture["expected_by_country"].get(where.get("country"))
                    if (
                        set(where) == {"country"}
                        and case.request.get("dimensions") == ["sales_pipeline.country"]
                    )
                    else None
                )
            else:
                expected = (
                    fixture["expected_revenue"]
                    if not value.get("where") and not case.request.get("dimensions")
                    else None
                )
            report["matching_cases"].append(
                {
                    "name": case.name,
                    "metric": name,
                    "expression": expr,
                    "matches_reference_expression": same,
                    "pins_reference_value": expected is not None
                    and value.get("metric") == name
                    and value.get("equals") == expected
                    and value.get("tolerance", 0) == 0,
                }
            )
    return report
