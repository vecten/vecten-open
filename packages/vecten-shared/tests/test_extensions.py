from types import SimpleNamespace

import pytest

from vecten_shared.config import VectenConfig
from vecten_shared.extensions import GROUPS, load_group, requirement, selected_entry_points
from vecten_shared.output import Failure


def config(*extensions):
    return VectenConfig.model_validate(
        {
            "project": {"name": "demo", "scaffold_version": "0.1.0"},
            "users": 2,
            "extensions": list(extensions),
        }
    )


def ep(name="demo", package="Acme_Demo", version="1.2", value=None):
    return SimpleNamespace(
        name=name,
        group="vecten.skills",
        dist=SimpleNamespace(name=package, version=version),
        load=lambda: value,
    )


def test_nine_groups():
    assert set(GROUPS) == {
        "vecten.connectors",
        "vecten.dbt_packages",
        "vecten.dags",
        "vecten.osi",
        "vecten.mcp.tools",
        "vecten.mcp.auth",
        "vecten.mcp.provisioning",
        "vecten.mcp.audit_sinks",
        "vecten.skills",
    }


def test_normalized_pinned_extras_and_exclusion():
    point = ep()
    assert selected_entry_points(config("acme.demo[extra]==1.2"), "vecten.skills", [point]) == [
        point
    ]
    assert selected_entry_points(config(), "vecten.skills", [point]) == []


@pytest.mark.parametrize(
    "text",
    [
        "--index-url https://secret",
        "thing\nother",
        "thing @ https://token@example.test/a.whl",
        "thing; python_version<'3'",
        "../thing",
    ],
)
def test_invalid_requirements_never_echo_input(text):
    with pytest.raises(Failure) as exc:
        requirement(text)
    assert text not in exc.value.message


def test_duplicate_and_version_mismatch():
    with pytest.raises(Failure, match="duplicate"):
        selected_entry_points(config("acme-demo"), "vecten.skills", [ep(), ep()])
    with pytest.raises(Failure, match="version"):
        selected_entry_points(config("acme-demo==2"), "vecten.skills", [ep()])


def test_safe_loading(monkeypatch):
    def fail():
        raise RuntimeError("private-token")

    point = ep()
    point.load = fail
    monkeypatch.setattr("vecten_shared.extensions.metadata.entry_points", lambda **kw: [point])
    with pytest.raises(Failure) as exc:
        load_group(config("acme-demo"), "vecten.skills")
    assert "private-token" not in exc.value.message
    assert "demo" in exc.value.message


def test_load_error_preserves_exception_type_and_cause(monkeypatch):
    error = ModuleNotFoundError("missing dependency")

    def fail():
        raise error

    point = ep()
    point.load = fail
    monkeypatch.setattr("vecten_shared.extensions.metadata.entry_points", lambda **kw: [point])
    with pytest.raises(Failure, match="ModuleNotFoundError") as exc:
        load_group(config("acme-demo"), "vecten.skills")
    assert exc.value.__cause__ is error
