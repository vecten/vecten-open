import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vecten.cli.app import app
from vecten.scaffold import render
from vecten_shared.provider.local import LOCAL_PG_PORT_ENV

from .docker_util import stack_pg_port


@pytest.fixture(scope="session", autouse=True)
def _render_each_answer_set_once(tmp_path_factory):
    """Copier's output is a function of the template and its answers alone, so a test gets the
    tree a fresh render would produce by copying the first render of the same answers.

    `render_project` always renders into a new empty directory and copies from it, so the
    cached tree is never handed to a test, only copied. The xdist workers of one run share the
    cache in the run's base directory: a tree is renamed into place whole, and a worker that
    loses the race to render the same answers drops its own, identical tree.
    """
    base = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        base = base.parent
    cache = base / "copier-renders"
    cache.mkdir(exist_ok=True)
    copier_run_copy = render.run_copy

    def run_copy(src_path, dst_path, **kwargs):
        key = json.dumps([src_path, kwargs], sort_keys=True, default=repr)
        tree = cache / hashlib.sha256(key.encode()).hexdigest()
        if not tree.is_dir():
            staging = Path(tempfile.mkdtemp(dir=cache))
            try:
                copier_run_copy(src_path, str(staging / "tree"), **kwargs)
                with contextlib.suppress(OSError):
                    (staging / "tree").rename(tree)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(tree, dst_path, symlinks=True, dirs_exist_ok=True)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(render, "run_copy", run_copy)
        yield


@pytest.fixture(autouse=True)
def _stack_pg_port(request, monkeypatch):
    """Each Docker test's stack publishes its Postgres on a port of its own, so none depends on
    55432 being free. `vecten up` and the host-side commands, which the tests run as
    subprocesses, read the port from the environment they inherit."""
    if request.node.get_closest_marker("docker"):
        monkeypatch.setenv(LOCAL_PG_PORT_ENV, str(stack_pg_port()))


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def invoke(runner, tmp_path, tmp_path_factory, monkeypatch):
    """Run the CLI inside tmp_path so no test touches the real filesystem.

    HOME lives outside tmp_path so the working directory starts empty, as `init` requires.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))

    def _invoke(*args, input=None, env=None):
        return runner.invoke(app, list(args), input=input, env=env, catch_exceptions=False)

    return _invoke


@pytest.fixture
def workdir(tmp_path) -> Path:
    return tmp_path
