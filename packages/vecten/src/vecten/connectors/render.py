"""Renders what every connector gets: the module, the test, the DAG files."""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

from jinja2 import Environment, FunctionLoader, StrictUndefined

from vecten_shared.records import write_text_atomic

GENERIC_SECRET_SUFFIX = {"rest": "api_key", "sql": "database_url", "filesystem": "bucket_url"}


def _template(name: str) -> str:
    return (
        resources.files("vecten.connectors").joinpath("templates", name).read_text(encoding="utf-8")
    )


_ENV = Environment(
    loader=FunctionLoader(_template), undefined=StrictUndefined, keep_trailing_newline=True
)


def scaffold_hash(connector_py: str) -> str:
    return hashlib.sha256(connector_py.encode("utf-8")).hexdigest()


def render_dag_file(
    name: str,
    group: str,
    objects: list[str],
    schedule: str = "@daily",
    timezone: str | None = None,
) -> str:
    return _ENV.get_template("sync_dag.py.j2").render(
        name=name, group=group, objects=objects, schedule=schedule, timezone=timezone
    )


def _write(target: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, content)


def render_test_file(target: Path, name: str) -> str:
    """Writes the replay test and fixtures directory every connector gets, whoever wrote it."""
    files = {
        f"test_{name}.py": _ENV.get_template("test_connector.py.j2").render(name=name),
        "fixtures/.gitkeep": "",
    }
    _write(target, files)
    return files[f"test_{name}.py"]


def render_connector_files(
    target: Path,
    name: str,
    kind: str,
    *,
    source_function: str | None,
    secrets: list[tuple[str, str]],
) -> dict[str, str]:
    """`secrets` pairs the source parameter with its secret name; generic kinds pass one pair."""
    template = {"dlt": "connector_verified.py.j2"}.get(kind, f"connector_{kind}.py.j2")
    connector_py = _ENV.get_template(template).render(
        name=name,
        source_function=source_function,
        secrets=secrets if kind == "dlt" else [s for _, s in secrets],
    )
    _write(target, {"__init__.py": "", "connector.py": connector_py})
    return {
        "__init__.py": "",
        "connector.py": connector_py,
        f"test_{name}.py": render_test_file(target, name),
        "fixtures/.gitkeep": "",
    }
