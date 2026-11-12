from types import SimpleNamespace

import pytest

from vecten_mcp.runtime_setup import LocalMcpSecrets
from vecten_shared.provider.runtime import RUNTIME_WRITABLE


def test_rotating_secrets_are_narrowly_scoped_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_STATE_DB_PASSWORD", "fixture-state")
    provider = LocalMcpSecrets(tmp_path)
    provider.put_secret("MCP_SIGNING_KEY", "keyring")
    assert LocalMcpSecrets(tmp_path).get_secret("MCP_SIGNING_KEY") == "keyring"
    assert provider.get_secret("MCP_STATE_DB_PASSWORD") == "fixture-state"
    assert (tmp_path / "MCP_SIGNING_KEY").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        provider.put_secret("LOADER_PASSWORD", "no")
    assert RUNTIME_WRITABLE == {
        "MCP_SIGNING_KEY",
        "MCP_IMPERSONATOR_PASSWORD",
    }


def test_default_backend_local_and_cloud_and_explicit_password():
    from vecten_mcp.runtime_setup import backend_name
    from vecten_shared.config import McpSection

    assert backend_name(McpSection(), "local") == "password"
    assert backend_name(McpSection(), "prod") == "oidc"
    assert backend_name(McpSection(auth="password"), "prod") == "password"


def test_maintenance_reconciles_every_fifteen_minutes_and_maintains_keys():
    from vecten_mcp.runtime import Runtime

    calls = []
    now = [1000]
    auth = SimpleNamespace(
        maintain=lambda: calls.append("auth"),
        clock=lambda: now[0],
        keys=SimpleNamespace(maintain=lambda: calls.append("keys")),
    )
    runtime = Runtime(
        None, auth, None, None, SimpleNamespace(expire=lambda: calls.append("expire"))
    )
    runtime.access = SimpleNamespace(reconcile=lambda: calls.append("reconcile"))
    runtime.tick()
    runtime.tick()
    assert calls.count("reconcile") == 1
    now[0] += 900
    runtime.tick()
    assert calls.count("reconcile") == 2
    # The key ring decides itself when to publish and when to switch.
    assert calls.count("keys") == 3


def _reconciling_runtime(now, calls, finished):
    from vecten_mcp.runtime import Runtime

    auth = SimpleNamespace(
        maintain=lambda: None, clock=lambda: now[0], keys=SimpleNamespace(maintain=lambda: None)
    )
    runtime = Runtime(None, auth, None, None, None)
    runtime.access = SimpleNamespace(reconcile=lambda: calls.append(now[0]))
    runtime.build_finished = finished
    return runtime


def test_a_recorded_build_reconciles_before_the_fifteen_minutes():
    now, calls, finished = [1000], [], ["2026-11-12T10:00:00+00:00\n"]
    runtime = _reconciling_runtime(now, calls, lambda: finished[0])
    runtime.tick()
    now[0] += 30
    runtime.tick()
    assert calls == [1000]
    finished[0] = "2026-11-12T10:05:00+00:00\n"
    now[0] += 30
    runtime.tick()
    now[0] += 30
    runtime.tick()
    assert calls == [1000, 1060]
    # The fifteen minutes count from the last reconciliation, whatever caused it.
    now[0] = 1060 + 899
    runtime.tick()
    now[0] += 1
    runtime.tick()
    assert calls == [1000, 1060, 1960]


def test_a_failed_reconciliation_after_a_build_is_retried_on_the_next_tick():
    now, calls, finished = [1000], [], ["one"]
    runtime = _reconciling_runtime(now, calls, lambda: finished[0])
    runtime.tick()
    finished[0] = "two"

    def unavailable():
        raise ValueError("warehouse unavailable")

    runtime.access.reconcile = unavailable
    now[0] += 30
    runtime.tick()
    runtime.access.reconcile = lambda: calls.append(now[0])
    now[0] += 30
    runtime.tick()
    assert calls == [1000, 1060]


def test_an_unreadable_build_record_leaves_the_schedule_alone(caplog):
    now, calls = [1000], []

    def unreadable():
        raise OSError("artifact store unavailable")

    runtime = _reconciling_runtime(now, calls, unreadable)
    runtime.tick()
    now[0] += 30
    runtime.tick()
    now[0] += 900
    runtime.tick()
    assert calls == [1000, 1930]
    assert "artifact store unavailable" in caplog.text


def test_an_unreadable_build_record_is_logged_once_until_it_reads_again(caplog):
    now, calls, state = [1000], [], {"error": True}

    def record():
        if state["error"]:
            raise PermissionError("AccessDenied: platform/dbt/build_finished_at")
        return "one"

    runtime = _reconciling_runtime(now, calls, record)
    for _ in range(3):
        runtime.tick()
        now[0] += 30
    assert caplog.text.count("AccessDenied") == 1
    state["error"] = False
    runtime.tick()
    state["error"] = True
    now[0] += 30
    runtime.tick()
    assert caplog.text.count("AccessDenied") == 2


def test_only_a_postgres_platform_watches_the_build_record():
    from vecten_mcp.runtime_setup import build_watch
    from vecten_shared.config import VectenConfig

    def config(kind, components):
        return VectenConfig.model_validate(
            {
                "project": {"name": "acme", "scaffold_version": "0.1.0"},
                "users": 3,
                "components": components,
                "warehouse": {"kind": kind},
            }
        )

    assert build_watch(config("postgres", ["platform", "mcp"])) is not None
    assert build_watch(config("snowflake", ["platform", "mcp"])) is None
    assert build_watch(config("postgres", ["mcp"])) is None


def test_local_backend_default_survives_config_serialization():
    from vecten_mcp.runtime_setup import backend_name
    from vecten_shared.config import McpSection

    saved = McpSection.model_validate(McpSection().model_dump())
    assert backend_name(saved, "local") == "password"
    assert (
        backend_name(McpSection(oidc_issuer="https://id.example", oidc_client_id="id"), "local")
        == "oidc"
    )


def test_failed_grant_reconciliation_does_not_skip_credential_rotation():
    from vecten_mcp.runtime import Runtime

    calls = []

    def unavailable():
        raise ValueError("grant authority unavailable")

    auth = SimpleNamespace(
        maintain=lambda: None,
        clock=lambda: 30 * 86400,
        keys=SimpleNamespace(maintain=lambda: calls.append("keys")),
    )
    runtime = Runtime(
        None,
        auth,
        None,
        None,
        None,
        access=SimpleNamespace(reconcile=unavailable),
        password_rotation=SimpleNamespace(tick=lambda: calls.append("password")),
    )
    runtime.tick()
    assert calls == ["password", "keys"]


def test_model_reloads_every_three_minutes_independently_of_reconciliation():
    from vecten_mcp.runtime import Runtime

    now, calls = [1000], []
    auth = SimpleNamespace(
        maintain=lambda: None, clock=lambda: now[0], keys=SimpleNamespace(maintain=lambda: None)
    )
    runtime = Runtime(None, auth, None, None, None)
    runtime.model = SimpleNamespace(reload=lambda: calls.append(now[0]))
    runtime.tick()
    runtime.tick()
    now[0] += 180
    runtime.tick()
    assert calls == [1000, 1180]


def test_retention_runs_under_bounded_database_function_once_daily():
    from vecten_mcp.runtime import Runtime

    now, calls = [100000], []
    auth = SimpleNamespace(
        maintain=lambda: None, clock=lambda: now[0], keys=SimpleNamespace(maintain=lambda: None)
    )
    state = SimpleNamespace(get=lambda *args: None, maintain_audit=lambda days: calls.append(days))
    runtime = Runtime(state, auth, None, None, None)
    runtime.audit = object()
    runtime.tick()
    runtime.tick()
    now[0] += 86400
    runtime.tick()
    assert calls == [90, 90]


def test_a_failed_maintenance_step_logs_its_cause(caplog):
    import logging

    from vecten_mcp.runtime import Runtime

    def unavailable():
        raise ValueError("grant authority unavailable at postgres://u:fixture-secret@h/db")

    auth = SimpleNamespace(
        maintain=lambda: None,
        clock=lambda: 30 * 86400,
        keys=SimpleNamespace(maintain=lambda: None),
    )
    runtime = Runtime(None, auth, None, None, None, access=SimpleNamespace(reconcile=unavailable))
    with caplog.at_level(logging.ERROR, logger="vecten_mcp.runtime"):
        runtime.tick()
    assert "ValueError: grant authority unavailable" in caplog.text
    assert "fixture-secret" not in caplog.text
