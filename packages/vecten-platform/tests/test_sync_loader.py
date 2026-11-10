import sys
from pathlib import Path

import pytest

from vecten_platform.sync.loader import load_connector
from vecten_shared.output import ExitCode, Failure

FIXTURES = Path(__file__).parent / "fixture_connector"


def test_loads_connector_module_exposing_source():
    module = load_connector(FIXTURES, "demo")
    assert callable(module.source)
    assert module.__name__ == "fixture_connector.demo.connector"
    assert str(FIXTURES.parent) in sys.path


def test_missing_connector_names_connector_add(tmp_path):
    (tmp_path / "connectors").mkdir()
    with pytest.raises(Failure) as exc:
        load_connector(tmp_path / "connectors", "stripe")
    assert exc.value.code == ExitCode.REFUSED
    assert "vecten connector add stripe" in exc.value.message


def test_connector_without_source_function_is_a_config_error(tmp_path):
    pkg = tmp_path / "connectors" / "nosrc"
    pkg.mkdir(parents=True)
    (tmp_path / "connectors" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "connector.py").write_text("X = 1\n")
    with pytest.raises(Failure) as exc:
        load_connector(tmp_path / "connectors", "nosrc")
    assert exc.value.code == ExitCode.CONFIG
    assert "source()" in exc.value.message


def _project(root: Path, marker: str) -> Path:
    connectors = root / "connectors"
    (connectors / "demo").mkdir(parents=True)
    (connectors / "__init__.py").write_text("")
    (connectors / "demo" / "__init__.py").write_text("")
    (connectors / "demo" / "connector.py").write_text(f"def source():\n    return {marker!r}\n")
    return connectors


def test_second_project_with_same_names_loads_its_own_code(tmp_path):
    first = _project(tmp_path / "one", "one")
    second = _project(tmp_path / "two", "two")
    assert load_connector(first, "demo").source() == "one"
    assert load_connector(second, "demo").source() == "two"
    for parent in (str(first.resolve().parent), str(second.resolve().parent)):
        assert sys.path.count(parent) == 1


def test_edited_connector_is_reimported(tmp_path):
    connectors = _project(tmp_path, "before")
    assert load_connector(connectors, "demo").source() == "before"
    (connectors / "demo" / "connector.py").write_text("def source():\n    return 'after'\n")
    assert load_connector(connectors, "demo").source() == "after"


def test_project_root_is_appended_after_existing_entries(tmp_path):
    connectors = _project(tmp_path, "x")
    before = list(sys.path)
    load_connector(connectors, "demo")
    parent = str(connectors.resolve().parent)
    assert sys.path[: len(before)] == before
    assert sys.path.index(parent) >= len(before)
