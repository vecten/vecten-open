"""Exercise BuildKit secret mounts with the real non-root runtime image users."""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from vecten.stack.compose import generate_compose, write_extension_dag_loader
from vecten.stack.images import write_project_layer
from vecten_shared.config import VectenConfig, save_config
from vecten_shared.versions import VERSIONS

from .docker_util import docker_available

pytestmark = pytest.mark.docker
REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("kind", ["airflow", "mcp"])
def test_extension_install_uses_ephemeral_build_secret(tmp_path, kind, monkeypatch):
    if not docker_available():
        pytest.skip("Docker is not available")
    base = f"vecten-{kind}:0.1.0"
    if subprocess.run(["docker", "image", "inspect", base], capture_output=True).returncode:
        pytest.skip(f"build {base} with vecten dev build-images first")
    config = VectenConfig.model_validate(
        {
            "project": {"name": "extension-test", "scaffold_version": "0.1.0"},
            "users": 2,
            "components": ["platform" if kind == "airflow" else "mcp"],
            "extensions": ["vecten-test-extension==1.0"],
            "packages": {"private_index": True},
        }
    )
    stack = tmp_path / ".vecten/stack"
    stack.mkdir(parents=True)
    save_config(tmp_path, config)
    (tmp_path / "requirements.txt").write_text("vecten-test-extension==1.0\n")
    for filename in ("requirements.lock", "requirements-airflow.lock", "requirements-mcp.lock"):
        (tmp_path / filename).write_text("vecten-test-extension==1.0\n")
    write_project_layer(stack, config, None)
    context = stack / "build"
    wheels = context / "wheels"
    subprocess.run(
        [
            "uv",
            "build",
            str(REPO / "packages/vecten/tests/fixtures/extension"),
            "--wheel",
            "--out-dir",
            str(wheels),
        ],
        check=True,
        capture_output=True,
    )
    if kind == "airflow":
        for package in ("vecten-shared", "vecten-platform"):
            subprocess.run(
                [
                    "uv",
                    "build",
                    str(REPO / "packages" / package),
                    "--wheel",
                    "--out-dir",
                    str(wheels),
                ],
                check=True,
                capture_output=True,
            )
        write_extension_dag_loader(stack)
        project_dags = tmp_path / "orchestration/dags"
        project_dags.mkdir(parents=True)
        (project_dags / "project.py").write_text(
            "from airflow.sdk import DAG\ndag = DAG('project_dag', schedule=None)\n"
        )
    from vecten.extensions import base_constraints, resolve_runtime_locks
    from vecten_shared.provider.local import LocalProvider

    baseline = base_constraints(kind)
    monkeypatch.setenv("UV_FIND_LINKS", str(wheels))
    LocalProvider(tmp_path, inside_compose=False).put_secret(
        "PACKAGE_INDEX_URL", "https://pypi.org/simple"
    )
    for filename, lock in resolve_runtime_locks(
        ["vecten-test-extension==1.0"], tmp_path, config
    ).items():
        (tmp_path / filename).write_text(lock)
        subprocess.run(
            [
                "uv",
                "tool",
                "run",
                "--python",
                VERSIONS.python,
                "--from",
                "pip",
                "pip",
                "download",
                "--no-deps",
                "--find-links",
                str(wheels),
                "--dest",
                str(wheels),
                "-r",
                str(tmp_path / filename),
            ],
            check=True,
            capture_output=True,
        )
    write_project_layer(stack, config, None)
    dockerfile = context / f"{kind}.Dockerfile"
    text = dockerfile.read_text()
    text = text.replace(
        f"FROM {base}\n",
        f"FROM {base}\nCOPY wheels/ /tmp/extension-wheels/\n"
        "ENV PIP_NO_INDEX=1 PIP_FIND_LINKS=/tmp/extension-wheels\n",
    )
    if kind == "airflow":
        text += (
            "RUN pip install --no-deps --force-reinstall /tmp/extension-wheels/vecten_shared*.whl "
            "/tmp/extension-wheels/vecten_platform*.whl\n"
            "ENV VECTEN_PROJECT_ROOT=/opt/airflow AIRFLOW__CORE__LOAD_EXAMPLES=False\n"
        )
    dockerfile.write_text(text)
    name = f"vecten-extension-test-{uuid.uuid4().hex[:10]}"
    secret = "https://fixture-user:fixture-build-token@example.invalid/simple"
    compose = {
        "services": {
            "test": {
                "image": name,
                "build": {
                    "context": str(context),
                    "dockerfile": dockerfile.name,
                    "secrets": [{"source": "index", "target": "package_index"}],
                },
            }
        },
        "secrets": {"index": {"environment": "VECTEN_TEST_INDEX"}},
    }
    file = tmp_path / "compose.yml"
    file.write_text(yaml.safe_dump(compose))
    try:
        build = subprocess.run(
            ["docker", "compose", "-f", str(file), "build"],
            env={**os.environ, "VECTEN_TEST_INDEX": secret},
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        assert secret not in build.stdout + build.stderr
        history = subprocess.check_output(["docker", "history", "--no-trunc", name], text=True)
        assert secret not in history
        inspect = json.loads(subprocess.check_output(["docker", "image", "inspect", name]))
        assert secret not in json.dumps(inspect)
        probe = (
            "import os, pathlib, vecten_test_extension; assert os.getuid() != 0; "
            "from importlib.metadata import version; "
            f"baseline = {baseline.splitlines()!r}; "
            "assert all(version(line.split('==')[0]) == line.split('==')[1] for line in baseline); "
            "assert not pathlib.Path('/run/secrets/package_index').exists(); "
            "assert not pathlib.Path('/tmp/vecten-pip.log').exists()"
        )
        mounts = []
        if kind == "airflow":
            doc = generate_compose(config, "local", ["platform"], stack)
            for volume in doc["services"]["airflow-dag-processor"]["volumes"]:
                source, target, *flags = volume.split(":")
                if target.startswith("/opt/airflow/dags") or target == "/opt/airflow/vecten.toml":
                    mounts += ["-v", ":".join([str((stack / source).resolve()), target, *flags])]
            before = {p.name: p.read_bytes() for p in project_dags.iterdir() if p.is_file()}
            probe += (
                "; from airflow.dag_processing.dagbag import DagBag; "
                "bag = DagBag('/opt/airflow/dags'); "
                "assert not bag.import_errors, bag.import_errors; "
                "expected = {'fixture_dag', 'fixture_decorated', 'project_dag'}; "
                "assert expected <= bag.dags.keys(), bag.dags; "
                "assert bag.dags['fixture_dag'].fileloc == '/opt/airflow/dags/vecten_extensions.py'"
            )
        run = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                *mounts,
                "--entrypoint",
                "python",
                name,
                "-c",
                probe,
            ],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        if kind == "airflow":
            assert {p.name: p.read_bytes() for p in project_dags.iterdir() if p.is_file()} == before
            task_run = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    *mounts,
                    "--entrypoint",
                    "bash",
                    name,
                    "-c",
                    "airflow db migrate >/tmp/migrate.log 2>&1 && "
                    "airflow dags test fixture_dag 2026-01-01 && "
                    "test -f /tmp/vecten-extension-task-ran",
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert task_run.returncode == 0, task_run.stdout[-12000:] + task_run.stderr[-4000:]
    finally:
        subprocess.run(["docker", "image", "rm", name], capture_output=True)
        shutil.rmtree(wheels, ignore_errors=True)
