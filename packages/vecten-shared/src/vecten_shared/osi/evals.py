"""Product-side eval cases: a query_metrics request and what it must compile to.

The cases live in evals/semantic.yaml, outside osi/, because dbt parses every JSON file under
osi/ as an Ossie document. Compiling needs no warehouse; an expected value needs data and is
checked only where fixture data exists.
"""

import datetime as dt
import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from vecten_shared.osi.issues import Issue, ModelError

EVALS_PATH = Path("evals") / "semantic.yaml"
VALUE_SKIPPED = "expected value checked only inside dbt-build-local"
OUTCOMES = ("compiled", "refused")


@dataclass(frozen=True)
class Case:
    name: str
    request: dict
    expect: dict
    gap: str | None = None
    question: str | None = None


@dataclass
class CaseResult:
    name: str
    ok: bool
    message: str
    gap: str | None = None
    sql: str | None = None
    skipped: str | None = None

    def render(self) -> str:
        gap = f" (gap {self.gap})" if self.gap else ""
        lines = [f"case {self.name}{gap}: {self.message}"]
        if self.sql:
            lines += [f"    {line}" for line in self.sql.splitlines()]
        return "\n".join(lines)


def sql_sha256(sql: str) -> str:
    """The hash of the compiled SQL text; bound values come from the case's own request."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _problem(where: str, message: str) -> Issue:
    return Issue("invalid_evals", f"{where}: {message}", EVALS_PATH.as_posix())


def _case_problem(raw: dict) -> str | None:
    request, expect = raw.get("request"), raw.get("expect")
    if not isinstance(request, dict):
        return "request must be a query_metrics request object"
    if not isinstance(expect, dict) or expect.get("outcome") not in OUTCOMES:
        return "expect.outcome must be compiled or refused"
    if expect["outcome"] == "compiled" and not isinstance(expect.get("sql_sha256"), str):
        return "a compiled case needs expect.sql_sha256"
    if expect["outcome"] == "refused" and not isinstance(expect.get("reason"), str):
        return "a refused case needs expect.reason"
    value = expect.get("value")
    if value is not None and not (
        isinstance(value, dict)
        and isinstance(value.get("metric"), str)
        and _finite(value.get("equals"))
        and _finite(value.get("tolerance", 0))
        and value.get("tolerance", 0) >= 0
        and isinstance(value.get("where", {}), dict)
        and all(isinstance(k, str) for k in value.get("where", {}))
    ):
        return (
            "expect.value needs a metric name, a finite numeric equals and an optional "
            "non-negative finite tolerance"
        )
    return None


def _finite(number) -> bool:
    return (
        isinstance(number, int | float) and not isinstance(number, bool) and math.isfinite(number)
    )


def _iso_dates(value):
    """YAML reads an unquoted 2024-01-01 as a date; requests and result rows compare strings."""
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _iso_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_iso_dates(v) for v in value]
    return value


def load_cases(path: Path) -> list[Case]:
    try:
        data = _iso_dates(yaml.safe_load(path.read_text(encoding="utf-8")))
    except UnicodeDecodeError:
        raise ModelError([_problem("file", "not UTF-8 text")]) from None
    except yaml.YAMLError as exc:
        raise ModelError([_problem("file", f"not valid YAML: {exc}")]) from None
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ModelError([_problem("file", "the root must be a mapping with a cases list")])
    issues: list[Issue] = []
    cases: list[Case] = []
    seen: set[str] = set()
    for index, raw in enumerate(data["cases"], start=1):
        name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(name, str) or not name:
            issues.append(_problem(f"case {index}", "every case needs a name"))
            continue
        if name in seen:
            issues.append(_problem(name, "duplicate case name"))
            continue
        seen.add(name)
        problem = _case_problem(raw)
        if problem:
            issues.append(_problem(name, problem))
            continue
        gap, question = raw.get("gap"), raw.get("question")
        cases.append(
            Case(
                name,
                raw["request"],
                raw["expect"],
                str(gap) if gap is not None else None,
                str(question) if question is not None else None,
            )
        )
    if issues:
        raise ModelError(issues)
    return cases


def _requested(request: dict) -> dict:
    time = request.get("time")
    return {
        "metrics": list(request.get("metrics") or []),
        "dimensions": list(request.get("dimensions") or []),
        "grain": time.get("grain") if isinstance(time, dict) else None,
    }


def run_case(model, case: Case) -> CaseResult:
    def fail(message, sql=None):
        return CaseResult(case.name, False, message, case.gap, sql)

    try:
        result = model.compile(case.request)
    except (ValueError, TypeError) as exc:
        return fail(f"the request is invalid: {exc}")
    expect = case.expect
    if result["resolution"] != expect["outcome"]:
        got = result.get("reason") or result["resolution"]
        return fail(f"expected {expect['outcome']}, got {got}", result.get("sql"))
    if expect["outcome"] == "refused":
        if result["reason"] != expect["reason"]:
            return fail(f"expected refusal {expect['reason']}, got {result['reason']}")
        return CaseResult(case.name, True, "passed", case.gap)
    requested = _requested(case.request)
    for key in ("metrics", "dimensions", "grain"):
        if key in expect and expect[key] != requested[key]:
            return fail(f"expect.{key} is {expect[key]!r} but the request asks {requested[key]!r}")
    digest = sql_sha256(result["sql"])
    if digest != expect["sql_sha256"]:
        return fail(
            f"sql_sha256 is now {digest}; update the case if the change is deliberate",
            result["sql"],
        )
    skipped = VALUE_SKIPPED if "value" in expect else None
    return CaseResult(case.name, True, "passed", case.gap, None, skipped)


def run_cases(model, cases: list[Case]) -> list[CaseResult]:
    return [run_case(model, case) for case in cases]


def _value_result(case: Case, value: dict, columns: list[str], rows: list) -> CaseResult:
    def fail(message):
        return CaseResult(case.name, False, message, case.gap)

    where = value.get("where") or {}
    index = {name: i for i, name in enumerate(columns)}
    missing = [c for c in [*where, value["metric"]] if c not in index]
    if missing:
        return fail(f"the result has no column {', '.join(missing)}")
    matching = [r for r in rows if all(_cell(r[index[k]]) == _cell(v) for k, v in where.items())]
    if len(matching) != 1:
        return fail(f"value.where matched {len(matching)} rows; it must match one")
    got = _cell(matching[0][index[value["metric"]]])
    tolerance = float(value.get("tolerance", 0))
    # A NaN result compares false with everything, so it must be rejected before the bound.
    if not _finite(got) or abs(got - float(value["equals"])) > tolerance:
        return fail(f"{value['metric']} is {got}, expected {value['equals']} ± {tolerance}")
    return CaseResult(case.name, True, "value matches", case.gap)


def _cell(value):
    """A result cell or a YAML value in one comparable form.

    Drivers return a truncated time bucket as a date or a midnight timestamp, often with a
    zone, while the case names it as an ISO date string.
    """
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        return float(value)
    return value


def check_values(model, cases: list[Case], execute) -> list[CaseResult]:
    """Runs each case that carries an expected value; `execute(sql, parameters)` returns
    the column names and rows of the warehouse's answer."""
    results = []
    for case in cases:
        value = case.expect.get("value")
        if not isinstance(value, dict):
            continue
        compiled = run_case(model, case)
        if not compiled.ok:
            results.append(compiled)
            continue
        query = model.compile(case.request)
        try:
            columns, rows = execute(query["sql"], query["parameters"])
        except Exception as exc:
            # Any driver error belongs to this case; the build reports it by name.
            message = f"the warehouse rejected the query: {exc}"
            results.append(CaseResult(case.name, False, message, case.gap, query["sql"]))
            continue
        results.append(_value_result(case, value, columns, rows))
    return results
