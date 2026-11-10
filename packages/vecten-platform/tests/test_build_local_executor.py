import sys
import types
from pathlib import Path

import pytest

from vecten_platform.dbt.build_local import _executor
from vecten_platform.dbt.local_warehouse import LocalTarget, disposable_postgres
from vecten_shared.provider import ConnectionSpec

from .docker_util import docker_available


class _Cursor:
    def __init__(self, calls):
        self.calls, self.description = calls, [types.SimpleNamespace(name="n")]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, parameters):
        self.calls.append((sql, parameters))

    def fetchall(self):
        return [(1,)]


class _Connection:
    def __init__(self, calls):
        self.calls = calls

    def cursor(self):
        return _Cursor(self.calls)

    def close(self):
        pass


def test_postgres_values_without_parameters_skip_placeholder_formatting(monkeypatch):
    # psycopg2 applies %-formatting whenever parameters are passed, even an empty list, so a
    # literal % in a metric expression must reach the driver with parameters None.
    calls = []
    driver = types.SimpleNamespace(connect=lambda **kwargs: _Connection(calls))
    monkeypatch.setitem(sys.modules, "psycopg2", driver)
    spec = ConnectionSpec(
        kind="postgres", database="warehouse", role="vecten", auth="password", host="h", port=1
    )
    target = LocalTarget("postgres", spec, "pw", {}, False, Path("."))
    execute = _executor(target)
    assert execute("SELECT 'a%'", []) == (["n"], [(1,)])
    assert execute("SELECT %s", [3]) == (["n"], [(1,)])
    assert execute("SELECT 101 % 100 WHERE 1 = %s", [1]) == (["n"], [(1,)])
    assert calls == [
        ("SELECT 'a%'", None),
        ("SELECT %s", [3]),
        ("SELECT 101 %% 100 WHERE 1 = %s", [1]),
    ]


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="docker not reachable")
def test_postgres_values_with_literal_percents_run_on_a_real_database():
    with disposable_postgres("percent-test") as target:
        execute = _executor(target)
        assert execute("SELECT 101 % 100 AS m WHERE 1 = %s", [1]) == (["m"], [(1,)])
        assert execute("SELECT 'P1' LIKE 'P%' AS m WHERE 'a' = %s", ["a"]) == (["m"], [(True,)])
        assert execute("SELECT 'P1' LIKE 'P%' AS m", []) == (["m"], [(True,)])
