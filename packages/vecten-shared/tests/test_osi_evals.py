import json
import shutil
from pathlib import Path

import pytest
import yaml

from vecten_shared.osi.evals import VALUE_SKIPPED, load_cases, run_cases, sql_sha256
from vecten_shared.osi.issues import ModelError
from vecten_shared.osi.semantic import SemanticModel

FIX = Path(__file__).parent / "fixtures" / "osi"


@pytest.fixture
def model(tmp_path):
    shutil.copytree(FIX, tmp_path / "osi")
    return SemanticModel.load(tmp_path / "osi", kind="postgres", commit="c1")


def first_metric():
    doc = json.loads((FIX / "metrics.json").read_text())
    return doc["semantic_model"][0]["metrics"][0]["name"]


def write(tmp_path, cases):
    path = tmp_path / "semantic.yaml"
    path.write_text(yaml.safe_dump({"cases": cases}))
    return path


def compiled_case(model, name="c1", **extra):
    request = {"metrics": [first_metric()]}
    sql = model.compile(request)["sql"]
    expect = {
        "outcome": "compiled",
        "metrics": [first_metric()],
        "dimensions": [],
        "grain": None,
        "sql_sha256": sql_sha256(sql),
        **extra,
    }
    return {"name": name, "request": request, "expect": expect}


def test_compiled_case_passes(model, tmp_path):
    results = run_cases(model, load_cases(write(tmp_path, [compiled_case(model)])))
    assert [(r.name, r.ok) for r in results] == [("c1", True)]


def test_drift_fails_naming_case_gap_and_new_sql(model, tmp_path):
    case = compiled_case(model)
    case["expect"]["sql_sha256"] = "0" * 64
    case["gap"] = "gap_7"
    (result,) = run_cases(model, load_cases(write(tmp_path, [case])))
    assert not result.ok and result.gap == "gap_7"
    assert result.sql == model.compile(case["request"])["sql"]
    assert "sql_sha256" in result.message


def test_refused_case(model, tmp_path):
    case = {
        "name": "r",
        "request": {"metrics": ["no_such_metric"]},
        "expect": {"outcome": "refused", "reason": "unknown_metric"},
    }
    assert run_cases(model, load_cases(write(tmp_path, [case])))[0].ok
    case["expect"]["reason"] = "join_required"
    result = run_cases(model, load_cases(write(tmp_path, [case])))[0]
    assert not result.ok and "unknown_metric" in result.message


def test_compiled_case_that_now_refuses(model, tmp_path):
    case = compiled_case(model)
    case["request"] = {"metrics": ["no_such_metric"]}
    case["expect"]["metrics"] = ["no_such_metric"]
    result = run_cases(model, load_cases(write(tmp_path, [case])))[0]
    assert not result.ok and "unknown_metric" in result.message


def test_request_and_expectation_must_agree(model, tmp_path):
    case = compiled_case(model)
    case["expect"]["grain"] = "month"
    result = run_cases(model, load_cases(write(tmp_path, [case])))[0]
    assert not result.ok and "grain" in result.message


def test_invalid_request_shape_fails_the_case(model, tmp_path):
    case = {
        "name": "bad",
        "request": {"metrics": "not-a-list"},
        "expect": {"outcome": "refused", "reason": "unknown_field"},
    }
    result = run_cases(model, load_cases(write(tmp_path, [case])))[0]
    assert not result.ok and "invalid" in result.message


def test_value_is_skipped_with_a_note(model, tmp_path):
    case = compiled_case(model, value={"metric": first_metric(), "equals": 1, "tolerance": 0})
    (result,) = run_cases(model, load_cases(write(tmp_path, [case])))
    assert result.ok and result.skipped == VALUE_SKIPPED


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("- just a list", "cases"),
        ("cases: [{name: a}]", "a: request"),
        ("cases: [{name: a, request: {}, expect: {outcome: maybe}}]", "outcome"),
        (
            "cases: [{name: a, request: {}, expect: {outcome: refused, reason: x}},"
            " {name: a, request: {}, expect: {outcome: refused, reason: x}}]",
            "duplicate",
        ),
        ("cases: [{request: {}, expect: {outcome: refused, reason: x}}]", "case 1"),
        ("cases: [{name: a, request: {}, expect: {outcome: compiled}}]", "sql_sha256"),
        ("cases: [{name: a, request: {}, expect: {outcome: refused}}]", "reason"),
        (
            "cases: [{name: a, request: {}, expect: {outcome: compiled, sql_sha256: x,"
            " value: {equals: 1}}}]",
            "value",
        ),
        ("cases: [: bad", "YAML"),
    ],
)
def test_malformed_file_names_the_case(tmp_path, text, fragment):
    path = tmp_path / "semantic.yaml"
    path.write_text(text)
    with pytest.raises(ModelError) as info:
        load_cases(path)
    assert fragment in str(info.value)


def test_check_values_with_a_fake_warehouse(model, tmp_path):
    from vecten_shared.osi.evals import check_values

    metric = first_metric()
    good = compiled_case(
        model, name="ok", value={"metric": metric, "equals": 100.0, "tolerance": 0.5}
    )
    bad = compiled_case(model, name="off", value={"metric": metric, "equals": 7})
    plain = compiled_case(model, name="plain")
    cases = load_cases(write(tmp_path, [good, bad, plain]))
    calls = []

    def execute(sql, parameters):
        calls.append(sql)
        return [metric], [(100.2,)]

    results = {r.name: r for r in check_values(model, cases, execute)}
    assert set(results) == {"ok", "off"} and len(calls) == 2
    assert results["ok"].ok and not results["off"].ok
    assert "100.2" in results["off"].message


def test_check_values_where_must_match_one_row(model, tmp_path):
    from vecten_shared.osi.evals import check_values

    metric = first_metric()
    case = compiled_case(model, value={"metric": metric, "equals": 1, "where": {"x": "a"}})
    cases = load_cases(write(tmp_path, [case]))
    (result,) = check_values(model, cases, lambda sql, p: (["x", metric], [("b", 1)]))
    assert not result.ok and "0 rows" in result.message
    (found,) = check_values(model, cases, lambda sql, p: (["x", metric], [("a", 1), ("b", 2)]))
    assert found.ok


def test_check_values_names_a_missing_column(model, tmp_path):
    from vecten_shared.osi.evals import check_values

    case = compiled_case(model, value={"metric": "nope", "equals": 1})
    (result,) = check_values(
        model, load_cases(write(tmp_path, [case])), lambda sql, p: (["x"], [(1,)])
    )
    assert not result.ok and "nope" in result.message


@pytest.mark.parametrize(
    "request_",
    [
        {"metrics": "not-a-list"},
        {"metrics": ["M"], "filters": [{"field": "F", "op": ["="], "value": 1}]},
        {"metrics": ["M"], "order_by": [{"field": "M", "dir": ["asc"]}]},
    ],
)
def test_a_case_the_compiler_cannot_take_fails_by_name(model, tmp_path, request_):
    field = next(iter(model.fields(model.homes[first_metric()])))
    text = json.dumps(request_).replace('"M"', json.dumps(first_metric()))
    request_ = json.loads(text.replace('"F"', json.dumps(field)))
    case = {
        "name": "odd",
        "request": request_,
        "expect": {"outcome": "refused", "reason": "unknown_field"},
    }
    (result,) = run_cases(model, load_cases(write(tmp_path, [case])))
    assert result.name == "odd" and not result.ok and "invalid" in result.message


def test_a_warehouse_error_fails_the_case_with_its_sql(model, tmp_path):
    from vecten_shared.osi.evals import check_values

    case = compiled_case(model, value={"metric": first_metric(), "equals": 1})

    def broken(sql, parameters):
        raise RuntimeError("relation does not exist")

    (result,) = check_values(model, load_cases(write(tmp_path, [case])), broken)
    assert not result.ok and "relation does not exist" in result.message
    assert result.sql == model.compile(case["request"])["sql"]


def test_a_non_numeric_metric_value_fails_the_case(model, tmp_path):
    from vecten_shared.osi.evals import check_values

    case = compiled_case(model, value={"metric": first_metric(), "equals": 1})
    cases = load_cases(write(tmp_path, [case]))
    (result,) = check_values(model, cases, lambda sql, p: ([first_metric()], [("abc",)]))
    assert not result.ok and "abc" in result.message


def test_where_keys_must_be_column_names(tmp_path):
    path = tmp_path / "semantic.yaml"
    path.write_text(
        "cases: [{name: a, request: {}, expect: {outcome: compiled, sql_sha256: x,"
        " value: {metric: m, equals: 1, where: {2024: 1}}}}]"
    )
    with pytest.raises(ModelError, match="value"):
        load_cases(path)


def test_a_file_that_is_not_utf8_is_a_model_error(tmp_path):
    path = tmp_path / "semantic.yaml"
    path.write_bytes(b"cases: [\xff\xfe]\n")
    with pytest.raises(ModelError, match="UTF-8"):
        load_cases(path)


def test_yaml_dates_reach_the_compiler_as_iso_strings(tmp_path):
    path = tmp_path / "semantic.yaml"
    path.write_text(
        "cases: [{name: a, request: {metrics: [m], time: {grain: month, start: 2024-01-01}},"
        " expect: {outcome: compiled, sql_sha256: x,"
        " value: {metric: m, equals: 1, where: {month_start: 2024-01-01}}}}]"
    )
    (case,) = load_cases(path)
    assert case.request["time"]["start"] == "2024-01-01"
    assert case.expect["value"]["where"] == {"month_start": "2024-01-01"}


def test_where_matches_a_time_bucket_returned_as_a_timestamp(model, tmp_path):
    import datetime as dt
    from decimal import Decimal

    from vecten_shared.osi.evals import check_values

    metric = first_metric()
    value = {"metric": metric, "equals": 5, "where": {"created_at": "2024-02-01"}}
    case = compiled_case(model, value=value)
    rows = [
        (dt.datetime(2024, 1, 1, tzinfo=dt.UTC), Decimal("3")),
        (dt.datetime(2024, 2, 1, tzinfo=dt.UTC), Decimal("5")),
    ]
    cases = load_cases(write(tmp_path, [case]))
    (result,) = check_values(model, cases, lambda sql, p: (["created_at", metric], rows))
    assert result.ok, result.message


@pytest.mark.parametrize("got", [float("nan"), float("inf")])
def test_a_non_finite_result_never_matches(model, tmp_path, got):
    from vecten_shared.osi.evals import check_values

    case = compiled_case(model, value={"metric": first_metric(), "equals": 100})
    cases = load_cases(write(tmp_path, [case]))
    (result,) = check_values(model, cases, lambda sql, p: ([first_metric()], [(got,)]))
    assert not result.ok


@pytest.mark.parametrize(
    "value",
    [
        "{metric: m, equals: .nan}",
        "{metric: m, equals: 1, tolerance: .inf}",
        "{metric: m, equals: 1, tolerance: -1}",
    ],
)
def test_non_finite_or_negative_expectations_are_refused(tmp_path, value):
    path = tmp_path / "semantic.yaml"
    path.write_text(
        "cases: [{name: a, request: {}, expect: {outcome: compiled, sql_sha256: x,"
        f" value: {value}}}}}]"
    )
    with pytest.raises(ModelError, match="value"):
        load_cases(path)
