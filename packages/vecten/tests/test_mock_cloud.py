"""The mocked cloud: a ControlPlane on the filesystem, selected through VECTEN_CONTROL_PLANE."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vecten.deploy import control
from vecten.github import gh as gh_mod
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import _REGISTRY

from .deploy_fixtures import COMMIT, FakeGh, FakeGit, render_aws, set_facts

MOCK_DIR = Path(__file__).parent / "fixtures" / "mock_cloud"


class RecordingTerraform:
    created = 0

    def __init__(self):
        type(self).created += 1

    def which(self):
        raise AssertionError("ship ran terraform")

    def run(self, *args, **kwargs):
        raise AssertionError("ship ran terraform")


@pytest.fixture
def mock_cloud(tmp_path, monkeypatch):
    cloud = tmp_path / "cloud"
    monkeypatch.syspath_prepend(str(MOCK_DIR))
    monkeypatch.setenv("VECTEN_CONTROL_PLANE", "vecten_mock_cloud:MockCloud")
    monkeypatch.setenv("VECTEN_MOCK_CLOUD_DIR", str(cloud))
    # register_cli_provider fills the provider registry for the whole process.
    monkeypatch.setattr("vecten_shared.provider._REGISTRY", dict(_REGISTRY))
    return cloud


def test_seam_selects_the_named_class(mock_cloud, tmp_path):
    plane = control.get_control_plane("aws", tmp_path, "prod")
    assert type(plane).__name__ == "MockCloud"
    assert isinstance(plane, control.ControlPlane)


@pytest.mark.parametrize("value", ["nomodule_xyz:Cls", "vecten_mock_cloud:Nope", "no-colon"])
def test_seam_refuses_a_bad_value_naming_the_variable(mock_cloud, tmp_path, monkeypatch, value):
    monkeypatch.setenv("VECTEN_CONTROL_PLANE", value)
    with pytest.raises(Failure) as caught:
        control.get_control_plane("aws", tmp_path, "prod")
    assert caught.value.code == ExitCode.USAGE
    assert "VECTEN_CONTROL_PLANE" in caught.value.message


def test_outputs_and_secrets_survive_a_new_instance(mock_cloud, tmp_path):
    from vecten_mock_cloud import MockCloud

    root = render_aws(tmp_path / "proj")
    plane = MockCloud(root, "prod")
    plane.store_outputs(MockCloud.OUTPUTS)
    plane.put_secret("DEMO_API_KEY", "fixture")
    again = MockCloud(root, "prod")
    assert again.stored_outputs().hosts.keys() == {"platform", "mcp"}
    assert again.outputs().setup_host == "mcp"
    assert again.get_secret("DEMO_API_KEY") == "fixture"
    assert again.list_secrets() == ["DEMO_API_KEY"]
    assert MockCloud(root, "staging").stored_outputs() is None


def test_ship_inside_actions_places_one_release_per_host_without_terraform(
    invoke, mock_cloud, tmp_path, monkeypatch
):
    from vecten_mock_cloud import MockCloud

    root = render_aws(tmp_path / "proj")
    monkeypatch.chdir(root)
    set_facts(root, bootstrapped=True, state_bucket="mock-state", deployed_commit=COMMIT)
    MockCloud(root, "prod").store_outputs(MockCloud.OUTPUTS)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("VECTEN_TERRAFORM_RUNNER", f"{__name__}:RecordingTerraform")
    RecordingTerraform.created = 0
    monkeypatch.setattr(gh_mod, "get_git", lambda: FakeGit())
    monkeypatch.setattr(gh_mod, "get_gh", lambda: FakeGh())
    result = invoke("ship", "--env", "prod", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["hosts"] == ["mcp", "platform"]
    assert RecordingTerraform.created == 0
    for host in ("mcp", "platform"):
        release = MockCloud.release(mock_cloud, host)
        assert release.name == COMMIT
        assert (release / "vecten.toml").is_file()
        assert json.loads((release / ".vecten/ship/host.json").read_text())["commit"] == COMMIT
    lines = (mock_cloud / "ships.jsonl").read_text().splitlines()
    assert [json.loads(line)["host"] for line in lines] == ["mcp", "platform"]


def test_another_process_reaches_the_mocked_cloud(mock_cloud, tmp_path):
    root = render_aws(tmp_path / "proj")
    set_facts(root, bootstrapped=True, state_bucket="mock-state")
    pythonpath = [str(MOCK_DIR), *filter(None, [os.environ.get("PYTHONPATH")])]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(pythonpath)}
    result = subprocess.run(
        [sys.executable, "-m", "vecten.cli.app", "secret", "set", "DEMO_API_KEY"]
        + ["--env", "prod", "--json"],
        cwd=root,
        env=env,
        input="from-another-process\n",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    stored = json.loads((mock_cloud / "secrets" / "prod.json").read_text())
    assert stored == {"DEMO_API_KEY": "from-another-process"}


def test_read_object_reads_the_mocked_bucket(mock_cloud, tmp_path):
    plane = control.get_control_plane("aws", tmp_path, "prod")
    (mock_cloud / "bucket" / "platform" / "dbt").mkdir(parents=True)
    (mock_cloud / "bucket" / "platform" / "dbt" / "built_commit").write_bytes(b"abc\n")
    assert plane.read_object("platform/dbt/built_commit") == b"abc\n"
    assert plane.read_object("platform/missing") is None
    assert plane.read_object("../outside") is None
