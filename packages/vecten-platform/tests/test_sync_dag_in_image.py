import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.docker


def docker_ok() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
def test_sync_dag_tests_pass_inside_the_airflow_image(tmp_path):
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
    from vecten.stack.images import base_image

    wheels = sorted(
        str(p.relative_to(REPO))
        for p in (REPO / "dist").glob("*.whl")
        if "vecten_platform" in p.name or "vecten_shared" in p.name
    )
    script = (
        "set -e; export AIRFLOW_HOME=/tmp/af; export AIRFLOW__CORE__LOAD_EXAMPLES=False; "
        "pip install --quiet --no-cache-dir pytest duckdb "
        + " ".join(f"/src/{w}" for w in wheels)
        + " && "
        "airflow db migrate >/dev/null && "
        "cd /tmp && python -m pytest -p no:cacheprovider -q "
        "/src/packages/vecten-platform/tests/test_sync_airflow_dag.py "
        "-o 'filterwarnings=ignore' --import-mode=importlib --tb=short --show-capture=no"
    )
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{REPO}:/src:ro",
            "-e",
            "VECTEN_PROJECT_ROOT=/tmp",
            "-e",
            "RUNTIME__DLTHUB_TELEMETRY=false",
            "--entrypoint",
            "bash",
            base_image("airflow", None),
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=1500,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    # The image has Airflow and the docker-marked dag.test() cases need nothing more, so a
    # skip there means a test silently stopped exercising the DAG.
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    assert "passed" in summary, proc.stdout[-4000:]
    assert not any(word in summary for word in ("failed", "error", "skipped")), summary
