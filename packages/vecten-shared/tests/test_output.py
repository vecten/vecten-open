import json
from pathlib import Path

from vecten_shared.output import ExitCode, Failure, Result, render


def test_exit_codes_are_the_documented_table():
    assert [c.value for c in ExitCode] == [0, 1, 2, 3, 4, 5, 6]
    assert ExitCode.USAGE != ExitCode.TOOL_FAILED


def test_result_renders_json_and_text_from_one_object():
    r = Result(data={"airflow_url": "https://airflow.localhost:8443"}, message="Stack is up")
    out, code = render(r, as_json=True)
    assert json.loads(out) == {"ok": True, "airflow_url": "https://airflow.localhost:8443"}
    assert code == 0
    out, code = render(r, as_json=False)
    assert out == "Stack is up"


def test_failure_renders_json_error_with_code():
    f = Failure(ExitCode.USAGE, "users must be a positive integer", details={"field": "users"})
    out, code = render(f, as_json=True)
    assert json.loads(out) == {
        "ok": False,
        "error": "users must be a positive integer",
        "code": 2,
        "code_name": "USAGE",
        "field": "users",
    }
    assert code == 2


def test_failure_details_with_non_json_types_still_render():
    f = Failure(ExitCode.REFUSED, "x", details={"path": Path("/tmp/p")})
    out, code = render(f, as_json=True)
    assert json.loads(out)["path"] == "/tmp/p"
    assert code == 4


def test_envelope_keys_win_over_payload_keys():
    out, _ = render(Result(data={"ok": False}), as_json=True)
    assert json.loads(out)["ok"] is True
    f = Failure(ExitCode.USAGE, "real", details={"ok": True, "error": "fake", "code": 0})
    doc = json.loads(render(f, as_json=True)[0])
    assert doc["ok"] is False and doc["error"] == "real" and doc["code"] == 2
