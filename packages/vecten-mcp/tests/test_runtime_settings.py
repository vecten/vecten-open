"""The MCP server checks every extension's settings before it starts serving."""

import pytest
import tomli_w

from vecten_shared.config import load_config
from vecten_shared.extension_record import Extension, record_text
from vecten_shared.output import ExitCode, Failure

SCHEMA = {"properties": {"daily_cap": {"type": "integer", "default": 1000}}}


def _project(root, settings):
    doc = {
        "users": 1,
        "project": {"name": "a", "scaffold_version": "0.1.0"},
        "extensions": ["acme"],
        "settings": {"acme": settings},
    }
    (root / "vecten.toml").write_text(tomli_w.dumps(doc))
    (root / "extensions.lock.json").write_text(
        record_text({"acme": Extension("1.0", (), settings=SCHEMA)})
    )
    return load_config(root)


def test_invalid_settings_stop_the_runtime_before_it_is_built(tmp_path, monkeypatch):
    from vecten_mcp import runtime, runtime_setup

    built = []
    monkeypatch.setattr(runtime_setup, "configured_runtime", lambda *a: built.append(a))
    monkeypatch.setenv("VECTEN_ENV", "prod")
    config = _project(tmp_path, {"env": {"prod": {"daily_cap": "many"}}})
    with pytest.raises(Failure) as caught:
        runtime.build_runtime(tmp_path, config, {})
    assert caught.value.code == ExitCode.CONFIG
    assert "settings.acme.daily_cap in prod" in caught.value.message
    assert built == []


def test_valid_settings_let_the_runtime_be_built(tmp_path, monkeypatch):
    from vecten_mcp import runtime, runtime_setup

    monkeypatch.setattr(runtime_setup, "configured_runtime", lambda *a: "runtime")
    monkeypatch.setenv("VECTEN_ENV", "prod")
    config = _project(tmp_path, {"daily_cap": 5})
    assert runtime.build_runtime(tmp_path, config, {}) == "runtime"
