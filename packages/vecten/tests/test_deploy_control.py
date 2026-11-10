"""The cloud-neutral control plane: the protocol every deploy cloud implements and its factory."""

import ast
from pathlib import Path

import pytest

from vecten.deploy import control, run
from vecten.deploy.control import SUPPORTED_CLOUDS, ControlPlane, get_control_plane
from vecten_shared.config import load_config
from vecten_shared.output import ExitCode, Failure

from .deploy_fixtures import render_aws


def test_aws_returns_an_aws_provider_satisfying_the_protocol(tmp_path):
    from vecten.deploy.aws import AwsProvider

    plane = get_control_plane("aws", tmp_path, "prod")
    assert isinstance(plane, AwsProvider)
    assert isinstance(plane, ControlPlane)
    assert plane.cloud == "aws"
    assert plane.env == "prod" and plane.project_root == tmp_path


def test_an_unknown_cloud_is_a_config_error_listing_the_supported_ones(tmp_path):
    with pytest.raises(Failure) as caught:
        get_control_plane("gcp", tmp_path, "prod")
    assert caught.value.code == ExitCode.CONFIG
    assert "'gcp'" in caught.value.message
    assert "'aws'" in caught.value.message and "'scaleway'" in caught.value.message
    assert SUPPORTED_CLOUDS == ("aws", "scaleway")


def test_scaleway_gets_a_scaleway_provider_satisfying_the_protocol(tmp_path):
    from vecten.deploy.scaleway import ScalewayProvider

    plane = get_control_plane("scaleway", tmp_path, "prod")
    assert isinstance(plane, ScalewayProvider)
    assert isinstance(plane, ControlPlane)
    assert plane.cloud == "scaleway"


def test_run_imports_no_aws_name_at_module_level():
    tree = ast.parse(Path(run.__file__).read_text(encoding="utf-8"))
    found = []
    # Only statements directly in the module body run at import; function bodies do not.
    for node in tree.body:
        for inner in ast.walk(node) if isinstance(node, ast.If) else [node]:
            if isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith(
                "vecten.deploy.aws"
            ):
                found.append(inner.module)
            if isinstance(inner, ast.Import):
                found += [a.name for a in inner.names if a.name.startswith("vecten.deploy.aws")]
    assert found == []
    assert "AwsProvider" not in Path(run.__file__).read_text(encoding="utf-8")


def test_require_deploy_cloud_accepts_both_clouds(tmp_path):
    for cloud in SUPPORTED_CLOUDS:
        root = render_aws(tmp_path / cloud, cloud=cloud)
        assert run.require_deploy_cloud(load_config(root)).cloud == cloud


def test_require_deploy_cloud_refuses_a_project_without_a_deploy_layer(tmp_path):
    from vecten.scaffold.params import ScaffoldParams
    from vecten.scaffold.render import render_project

    from .scaffold_matrix import LOCAL

    root = tmp_path / "local"
    render_project(root, ScaffoldParams.from_answers({**LOCAL, "sources": ["hubspot"]}, {}))
    with pytest.raises(Failure) as caught:
        run.require_deploy_cloud(load_config(root))
    assert caught.value.code == ExitCode.REFUSED
    assert "vecten init --deploy" in caught.value.message


def test_aws_backend_config_names_the_state_bucket_key_and_region(tmp_path):
    from vecten.deploy.terraform import TerraformRoot

    from .deploy_fixtures import REGION, STATE_BUCKET, set_facts

    root = render_aws(tmp_path / "proj")
    set_facts(root, bootstrapped=True, state_bucket=STATE_BUCKET)
    plane = get_control_plane("aws", root, "prod")
    tf = TerraformRoot("aws", root / "deploy/terraform/aws", "prod/aws.tfstate", "x.tfvars")
    assert plane.backend_config("prod", tf) == {
        "bucket": STATE_BUCKET,
        "key": "prod/aws.tfstate",
        "region": REGION,
        "use_lockfile": "true",
        "encrypt": "true",
    }
    assert plane.terraform_env() == {}
    assert plane.outputs_location() == f"s3://{STATE_BUCKET}/prod/outputs.json"


def test_cli_registration_is_per_cloud(tmp_path):
    from vecten.deploy.aws import AwsProvider
    from vecten_shared.provider import provider_factory

    control.register_cli_provider("aws")
    assert provider_factory("aws") is AwsProvider
    control.register_cli_provider("scaleway")
    from vecten.deploy.scaleway import ScalewayProvider

    assert provider_factory("scaleway") is ScalewayProvider
