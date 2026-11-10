"""A credential-free CLI journey from an empty directory to model-ready next output."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vecten.stack.compose import compose_project_name
from vecten_shared.config import load_config

from .docker_util import docker_available, remove_stack_images
from .fixture_project import OVERLAY, RAW
from .test_up_e2e import drop_volumes, vecten

pytestmark = pytest.mark.docker
REPO = Path(__file__).resolve().parents[3]


def run(root, *args):
    result = vecten(*args, "--json", cwd=root)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(not docker_available(), reason="Docker daemon unavailable")
def test_complete_laptop_pipeline(tmp_path):
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "vecten.cli.app",
            "dev",
            "build-images",
            "--repo",
            str(REPO),
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run(tmp_path, "init", "pipeline", "--name", "pipeline", "--users", "3", "--source", "demo")
    root = tmp_path / "pipeline"
    project = compose_project_name(root, load_config(root))
    drop_volumes(project)
    try:
        run(root, "up")
        run(root, "connector", "add", "demo", "--from", "rest")
        run(root, "answer", "access.demo", "default")
        connector = root / "connectors/demo/connector.py"
        connector.write_text(
            "import dlt\n\nROWS = " + repr(RAW) + "\n\n"
            '@dlt.source(name="demo")\ndef source():\n'
            '    return [dlt.resource(rows, name=name, write_disposition="replace")\n'
            "            for name, rows in ROWS.items()]\n"
        )
        run(root, "connector", "run", "demo", "--record")
        assert list((root / "connectors/demo/fixtures").glob("*.jsonl"))
        schema = run(root, "raw", "schema", "demo")
        assert "company" in json.dumps(schema) and "deal" in json.dumps(schema)
        shutil.copytree(OVERLAY, root, dirs_exist_ok=True)
        run(root, "dbt-build-local")
        run(root, "dbt-build-local", "--check")
        run(root, "mcp", "validate")
        state = run(root, "next")
        resolutions = [item["resolution"] for item in state["items"]]
        assert "connector" not in resolutions and "model" not in resolutions
        assert not any("connector run" in r or "dbt-build-local" in r for r in resolutions)
        document = json.loads((root / "osi/datasets.json").read_text())
        assert document
    finally:
        result = vecten("down", "--volumes", "--json", cwd=root)
        drop_volumes(project)
        remove_stack_images(project)
        assert result.returncode == 0, result.stdout + result.stderr
