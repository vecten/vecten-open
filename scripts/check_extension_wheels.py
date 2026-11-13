"""Check release wheels, including actual installed metadata for a ten-group fixture."""

import argparse
import ast
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

FORBIDDEN_MODULES = {"saml", "onelogin", "posthog", "segment", "sentry_sdk", "snowplow_tracker"}
FORBIDDEN_NAMES = {
    "license_key",
    "licence_key",
    "check_license",
    "check_licence",
    "telemetry_enabled",
}


def check_source(source: str, name: str) -> None:
    tree = ast.parse(source, filename=name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            names = []
        if set(names) & FORBIDDEN_MODULES:
            raise ValueError(f"{name}: Enterprise implementation or telemetry import")
        identifier = getattr(node, "id", None) or getattr(node, "name", None)
        if identifier in FORBIDDEN_NAMES:
            raise ValueError(f"{name}: licence gating or telemetry symbol")


def check_wheels(directory: Path) -> None:
    wheels = sorted(directory.glob("*.whl"))
    expected = {"vecten", "vecten_platform", "vecten_mcp", "vecten_shared", "vecten_test_extension"}
    if {path.name.split("-")[0] for path in wheels} != expected or len(wheels) != 5:
        raise ValueError("supply exactly the four release wheels and the test extension wheel")
    with tempfile.TemporaryDirectory(prefix="vecten-wheel-check-") as temp:
        target = Path(temp)
        for wheel in wheels:
            with zipfile.ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if name.endswith(".py") and not name.startswith("vecten_test_extension/"):
                        check_source(archive.read(name).decode(), name)
                archive.extractall(target)
        program = r"""
import importlib.metadata as metadata
import tempfile
from pathlib import Path
from vecten_shared.config import VectenConfig, save_config
from vecten_shared.extensions import GROUPS
from vecten_shared.osi.documents import list_documents, load_document, merge
from vecten_shared.osi.validate import validate_osi
from vecten.connectors.extensions import extension_connectors
from vecten.extension_skills import link_extension_skills
from vecten_platform.dbt.deps import extension_packages
from vecten_platform.extensions import dag_files
from vecten_mcp.app import create_app
from vecten_shared.output import Failure
config = VectenConfig.model_validate({
    "project": {"name": "wheel-check", "scaffold_version": "0.1.0"},
    "users": 2, "extensions": ["vecten-test-extension==1.0"], "mcp": {"auth": "fixture"},
})
points = metadata.distribution("vecten-test-extension").entry_points
assert {ep.group for ep in points} == set(GROUPS)
with tempfile.TemporaryDirectory() as project:
    root = Path(project)
    (root / "osi").mkdir()
    save_config(root, config)
    assert "fixture" in extension_connectors(config)
    assert extension_packages(config)
    assert [p.name for p in dag_files(config)] == ["fixture.py"]
    documents = [load_document(p) for p in list_documents(root / "osi")]
    assert "fixture_dataset" in merge(documents).datasets
    assert not validate_osi(root / "osi", warehouse_kind="postgres").errors
    registry = create_app(root).state.extensions
    assert registry.tools_for({"analyst"})["fixture"]() == "fixture"
    assert registry.identity_backend("fixture")({})["subject"] == "fixture"
    assert registry.provision("fixture", "alice") == "alice"
    registry.emit_audit({"receipt": "one"})
    from vecten_test_extension import rows
    assert rows == [{"receipt": "one"}]
    link_extension_skills(root, config)
    assert (root / "skills/fixture/SKILL.md").exists()
    config.mcp.auth = "saml"
    save_config(root, config)
    try:
        create_app(root)
    except Failure as exc:
        assert "saml" in exc.message and "not installed" in exc.message
    else:
        raise AssertionError("missing SAML extension accepted")
print("five wheels checked; all ten extension groups reached their owning layers")
"""
        subprocess.run(
            [sys.executable, "-c", program],
            env={**os.environ, "PYTHONPATH": str(target)},
            cwd=target,
            check=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    check_wheels(parser.parse_args().directory)
