"""`vecten answer deploy.promotion` records the promotion order in vecten.toml, or removes it."""

import json
import re
import tomllib

import pytest

from vecten_shared.config import PROMOTION_SINCE, load_config

from .fixture_project import render_fixture


def with_tfvars(root, *envs):
    prod = root / "deploy" / "envs" / "prod.tfvars"
    for env in envs:
        text = re.sub(r'(?m)^(environment\s*=\s*)"prod"', rf'\1"{env}"', prod.read_text())
        (root / "deploy" / "envs" / f"{env}.tfvars").write_text(text)
    return root


def rendered_by(root, version):
    path = root / "vecten.toml"
    path.write_text(
        path.read_text().replace("scaffold_version = ", f'scaffold_version = "{version}"\n# ', 1)
    )
    return root


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = rendered_by(
        with_tfvars(render_fixture(tmp_path, "postgres", deploy=True), "dev"), PROMOTION_SINCE
    )
    monkeypatch.chdir(root)
    return root


def deploy_table(root):
    return tomllib.loads((root / "vecten.toml").read_text())["deploy"]


def test_an_order_is_recorded_in_vecten_toml(invoke, project):
    r = invoke("answer", "deploy.promotion", "dev, prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout) == {
        "ok": True,
        "key": "deploy.promotion",
        "value": ["dev", "prod"],
        "file": "vecten.toml",
    }
    assert deploy_table(project)["promotion"] == ["dev", "prod"]
    assert load_config(project).promotion_order() == ("dev", "prod")
    text = invoke("answer", "deploy.promotion", "dev,prod")
    assert text.exit_code == 0
    assert "deploy.promotion = ['dev', 'prod']" in text.stdout


def test_none_removes_the_order_and_leaves_the_file_as_it_was(invoke, project):
    before = (project / "vecten.toml").read_text()
    assert invoke("answer", "deploy.promotion", "dev,prod").exit_code == 0
    r = invoke("answer", "deploy.promotion", "none", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["value"] is None
    assert "promotion" not in deploy_table(project)
    assert (project / "vecten.toml").read_text() == before
    assert load_config(project).promotion_order() == ("prod",)


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("dev,staging", "must end with prod"),
        ("prod,dev", "must end with prod"),
        ("dev,dev,prod", "names dev more than once"),
        ("prod", "at least two environments"),
        ("Dev,prod", "environment name"),
        (" , ", "deploy.promotion"),
    ],
)
def test_an_invalid_order_is_refused_and_nothing_is_written(invoke, project, value, problem):
    before = (project / "vecten.toml").read_text()
    r = invoke("answer", "deploy.promotion", value, "--json")
    assert r.exit_code == 2, r.stdout
    error = json.loads(r.stdout)["error"]
    assert "deploy.promotion" in error and problem in error
    assert (project / "vecten.toml").read_text() == before


def test_an_environment_without_tfvars_is_refused(invoke, project):
    before = (project / "vecten.toml").read_text()
    r = invoke("answer", "deploy.promotion", "staging,prod", "--json")
    assert r.exit_code == 2, r.stdout
    error = json.loads(r.stdout)["error"]
    assert "deploy.promotion" in error and "deploy/envs/staging.tfvars" in error
    assert (project / "vecten.toml").read_text() == before


def test_workflows_older_than_the_order_name_vecten_update(invoke, tmp_path, monkeypatch):
    root = with_tfvars(render_fixture(tmp_path, "postgres", deploy=True), "dev")
    rendered_by(root, "0.1.0")
    monkeypatch.chdir(root)
    before = (root / "vecten.toml").read_text()
    r = invoke("answer", "deploy.promotion", "dev,prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "vecten update" in json.loads(r.stdout)["error"]
    assert (root / "vecten.toml").read_text() == before


def test_a_project_without_a_deploy_layer_names_init_deploy(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    before = (root / "vecten.toml").read_text()
    r = invoke("answer", "deploy.promotion", "dev,prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "vecten init --deploy" in json.loads(r.stdout)["error"]
    assert (root / "vecten.toml").read_text() == before


def test_outside_a_project_the_key_is_refused(invoke, workdir):
    r = invoke("answer", "deploy.promotion", "dev,prod", "--json")
    assert r.exit_code == 4, r.stdout
    assert "not inside a vecten project" in json.loads(r.stdout)["error"]
    assert not (workdir / "vecten-answers.yml").exists()


def test_the_unknown_key_message_names_the_promotion_key(invoke, workdir):
    r = invoke("answer", "colour", "blue", "--json")
    assert r.exit_code == 2
    assert "deploy.promotion" in json.loads(r.stdout)["error"]
