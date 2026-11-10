import os

import pytest

from vecten_platform.sync import context
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider.local import LocalProvider


@pytest.fixture(autouse=True)
def reset_runtime():
    context.set_runtime(None)
    yield
    context.set_runtime(None)


def test_runtime_from_environment_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTEN_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("VECTEN_CONNECTORS_DIR", raising=False)
    monkeypatch.delenv("VECTEN_ENV", raising=False)
    monkeypatch.delenv("VECTEN_PROVIDER", raising=False)
    monkeypatch.delenv("VECTEN_DLT_DIR", raising=False)
    monkeypatch.setenv("VECTEN_INSIDE_COMPOSE", "1")
    ctx = context.runtime()
    assert ctx.project_root == tmp_path
    assert ctx.connectors_dir == tmp_path / "connectors"
    assert ctx.env == "local"
    assert isinstance(ctx.provider, LocalProvider) and ctx.provider.inside_compose is True
    assert ctx.dlt_dir == tmp_path / ".vecten" / "dlt"


def test_runtime_without_project_root_is_a_failure(monkeypatch):
    monkeypatch.delenv("VECTEN_PROJECT_ROOT", raising=False)
    with pytest.raises(Failure) as exc:
        context.runtime()
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "VECTEN_PROJECT_ROOT" in exc.value.message


def test_set_runtime_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VECTEN_PROJECT_ROOT", "/nowhere")
    ctx = context.RuntimeContext(
        project_root=tmp_path,
        connectors_dir=tmp_path / "connectors",
        env="local",
        provider=LocalProvider(tmp_path, inside_compose=False),
        dlt_dir=tmp_path / ".vecten" / "dlt",
    )
    context.set_runtime(ctx)
    assert context.runtime() is ctx


def test_secret_reads_through_the_runtime_provider(tmp_path):
    provider = LocalProvider(tmp_path, inside_compose=False)
    provider.put_secret("DEMO_TOKEN", "t0k")
    context.set_runtime(
        context.RuntimeContext(tmp_path, tmp_path / "connectors", "local", provider, tmp_path / "d")
    )
    assert context.secret("DEMO_TOKEN") == "t0k"


def test_importing_sync_turns_dlt_telemetry_off():
    import vecten_platform.sync  # noqa: F401

    assert os.environ.get("RUNTIME__DLTHUB_TELEMETRY") == "false"


def test_a_cloud_provider_is_bound_to_the_runtime_environment(tmp_path, monkeypatch):
    from vecten_shared.provider.aws import AwsRuntimeProvider

    monkeypatch.setenv("VECTEN_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VECTEN_PROVIDER", "aws")
    monkeypatch.setenv("VECTEN_ENV", "prod")
    ctx = context.runtime()
    assert ctx.env == "prod"
    assert isinstance(ctx.provider, AwsRuntimeProvider) and ctx.provider.env == "prod"
