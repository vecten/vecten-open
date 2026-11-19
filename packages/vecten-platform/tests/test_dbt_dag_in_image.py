import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vecten_shared.versions import VERSIONS

REPO = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.docker


def docker_ok() -> bool:
    return (
        shutil.which("docker") is not None
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    )


def in_image(image: str, script: str, *docker_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", *docker_args, "--entrypoint", "bash", image, "-c", script],
        capture_output=True,
        text=True,
        timeout=1500,
    )


@pytest.mark.skipif(not docker_ok(), reason="no docker daemon")
def test_dbt_dag_tests_pass_inside_the_airflow_image():
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

    image = base_image("airflow", None)

    imports = in_image(image, 'python -c "import dbt.cli.main, vecten_platform.dbt.airflow_dag"')
    assert imports.returncode == 0, imports.stdout[-4000:] + imports.stderr[-4000:]

    # The build mounts its own uv, so the image holds only the uv the upstream image ships, at
    # its version, and no lock file uv takes on an environment; the packages stay owned by the
    # user that runs them. As root, so no directory hides a file.
    probe = (
        "find / -xdev \\( -name uv -o -name .lock \\) ! -type d -print | sort;"
        " /home/airflow/.local/bin/uv --version; find /home/airflow/.local ! -user airflow"
    )
    built = in_image(image, probe, "--user", "0")
    upstream = in_image(VERSIONS.airflow_image, probe, "--user", "0")
    assert built.returncode == 0, built.stderr[-4000:]
    assert upstream.returncode == 0, upstream.stderr[-4000:]
    assert built.stdout == upstream.stdout
    assert "/home/airflow/.local/bin/uv" in built.stdout.split()

    # Compose runs Airflow as the host uid in group 0, and a fresh volume copies the image
    # directory's owner and mode.
    volume = in_image(
        image, "touch /opt/airflow/artifacts/probe", "-u", "4242:0", "-v", "/opt/airflow/artifacts"
    )
    assert volume.returncode == 0, volume.stdout[-4000:] + volume.stderr[-4000:]

    script = (
        "set -e; export AIRFLOW_HOME=/tmp/af; export AIRFLOW__CORE__LOAD_EXAMPLES=False; "
        "pip install --quiet --no-cache-dir pytest && "
        # dag.test() runs a DAG against Airflow's metadata database.
        "airflow db migrate >/dev/null && "
        "cd /tmp && python -m pytest -p no:cacheprovider -q "
        "/src/packages/vecten-platform/tests/test_dbt_airflow_dag.py "
        "-o 'filterwarnings=ignore' --import-mode=importlib --tb=short --show-capture=no"
    )
    proc = in_image(image, script, "-v", f"{REPO}:/src:ro")
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    summary = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    assert "passed" in summary, proc.stdout[-4000:]
    assert "failed" not in summary and "error" not in summary, proc.stdout[-4000:]
