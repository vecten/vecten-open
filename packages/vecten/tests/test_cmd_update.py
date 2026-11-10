import json
from dataclasses import replace

import pytest
import yaml

from vecten.scaffold import params as params_module
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config
from vecten_shared.versions import VERSIONS

BASELINE = ".copier-baseline.json"


def project(tmp_path, monkeypatch, *, deploy=False, version="0.0.9"):
    old = replace(VERSIONS, vecten=version, terraform_module_ref="v0.0.9")
    monkeypatch.setattr(params_module, "VERSIONS", old)
    answers = {"name": "demo", "users": 3, "sources": ["demo"]}
    if deploy:
        answers.update(
            cloud="aws",
            domain="data.example.com",
            dns_zone="example.com",
            github_repository="example/demo",
        )
    render_project(tmp_path, ScaffoldParams.from_answers(answers, {}))
    monkeypatch.setattr(params_module, "VERSIONS", VERSIONS)
    return tmp_path


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_update_release_preserves_user_work(invoke, tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch, deploy=True)
    cfg = root / "vecten.toml"
    cfg.write_text(cfg.read_text().replace("users = 3", "users = 7"))
    for rel in ["orchestration/dags/custom.py", "dbt/models/marts/custom.sql", ".env"]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("user contents\n")
    result = invoke("update", "--json")
    assert result.exit_code == 0, result.stdout
    doc = json.loads(result.stdout)
    assert doc["from_version"] == "0.0.9" and doc["to_version"] == VERSIONS.vecten
    assert load_config(root).users == 7
    assert load_config(root).project.scaffold_version == VERSIONS.vecten
    assert VERSIONS.terraform_module_ref in (root / "deploy/terraform/aws/main.tf").read_text()
    assert (
        yaml.safe_load((root / ".copier-answers.yml").read_text())["vecten_version"]
        == VERSIONS.vecten
    )
    for rel in ["orchestration/dags/custom.py", "dbt/models/marts/custom.sql", ".env"]:
        assert (root / rel).read_text() == "user contents\n"
    before = files(root)
    again = invoke("update", "--json")
    assert again.exit_code == 0, again.stdout
    assert json.loads(again.stdout)["changed"] == []
    assert files(root) == before


def test_conflict_does_not_change_any_project_file(invoke, tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch)
    p = root / "vecten.toml"
    p.write_text(p.read_text().replace('scaffold_version = "0.0.9"', 'scaffold_version = "0.0.8"'))
    before = files(root)
    result = invoke("update", "--json")
    assert result.exit_code == 4, result.stdout
    assert "vecten.toml" in json.loads(result.stdout)["conflicts"]
    assert files(root) == before


def test_unmodified_deleted_file_stays_deleted(invoke, tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch)
    (root / "orchestration/docker-compose.override.yml").unlink()
    result = invoke("update", "--json")
    assert result.exit_code == 0, result.stdout
    assert not (root / "orchestration/docker-compose.override.yml").exists()


@pytest.mark.parametrize(
    "name", ["../outside", "/tmp/outside", ".git/config", ".env", "foo/../../out"]
)
def test_bad_baseline_paths_refused(invoke, tmp_path, monkeypatch, name):
    root = project(tmp_path, monkeypatch)
    p = root / BASELINE
    data = json.loads(p.read_text())
    data["files"][name] = "bad"
    p.write_text(json.dumps(data))
    before = files(root)
    result = invoke("update", "--json")
    assert result.exit_code == 6, result.stdout
    assert files(root) == before


def test_symlink_target_refused(invoke, tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch)
    p = root / "README.md"
    p.unlink()
    p.symlink_to(root / "vecten.toml")
    before = files(root)
    result = invoke("update", "--json")
    assert result.exit_code == 4, result.stdout
    assert files(root) == before


def test_missing_baseline_from_older_release_refused(invoke, tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch)
    (root / BASELINE).unlink()
    before = files(root)
    result = invoke("update", "--json")
    assert result.exit_code == 4, result.stdout
    assert files(root) == before


def test_legacy_current_release_can_adopt(invoke, tmp_path):
    render_project(
        tmp_path, ScaffoldParams.from_answers({"name": "demo", "users": 3, "sources": []}, {})
    )
    (tmp_path / BASELINE).unlink()
    result = invoke("update", "--json")
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["adopted_baseline"] is True


def test_outside_project_refused(invoke):
    result = invoke("update", "--json")
    assert result.exit_code == 4, result.stdout


def test_init_deploy_advances_only_deploy_baseline(invoke, tmp_path, monkeypatch):
    assert (
        invoke(
            "init", "p", "--name", "demo", "--users", "3", "--source", "demo", "--json"
        ).exit_code
        == 0
    )
    root = tmp_path / "p"
    baseline = json.loads((root / BASELINE).read_text())
    (root / "docs/runbook.md").write_text("personal runbook\n")
    monkeypatch.chdir(root)
    result = invoke(
        "init",
        "--deploy",
        "--cloud",
        "aws",
        "--domain",
        "data.example.com",
        "--dns-zone",
        "example.com",
        "--github-repository",
        "example/demo",
        "--json",
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["hand_merge"] == ["docs/runbook.md"]
    after = json.loads((root / BASELINE).read_text())
    assert "deploy/terraform/aws/main.tf" in after["files"]
    assert after["files"]["docs/runbook.md"] == baseline["files"]["docs/runbook.md"]
    assert (root / "docs/runbook.md").read_text() == "personal runbook\n"
    result = invoke("update", "--json")
    assert result.exit_code == 4, result.stdout
    assert "docs/runbook.md" in json.loads(result.stdout)["conflicts"]


def test_module_only_change_preserves_tfvars(invoke, tmp_path, monkeypatch):
    root = project(tmp_path, monkeypatch, deploy=True, version=VERSIONS.vecten)
    p = root / "deploy/envs/prod.tfvars"
    p.write_text(p.read_text() + "\n# owner tuning\n")
    before = p.read_bytes()
    result = invoke("update", "--json")
    assert result.exit_code == 0, result.stdout
    assert p.read_bytes() == before
    changed = json.loads(result.stdout)["changed"]
    assert set(changed) <= {BASELINE, ".copier-answers.yml", "deploy/terraform/aws/main.tf"}


@pytest.mark.parametrize(
    "case", ["new_collision", "deleted_changed", "removed_edited", "disjoint", "renamed"]
)
def test_three_way_file_cases(tmp_path, case):
    from vecten.scaffold.merge import merge_snapshots

    middle = "".join(f"unchanged {n}\n" for n in range(10))
    old = {"a.txt": "first\n" + middle + "last\n"}
    new = {"a.txt": "new first\n" + middle + "last\n"}
    current = {"a.txt": "first\n" + middle + "my last\n"}
    if case == "new_collision":
        old = {}
        new = {"a.txt": "upstream\n"}
        current = {"a.txt": "mine\n"}
    elif case == "deleted_changed":
        current = {}
    elif case == "removed_edited":
        new = {}
    elif case == "renamed":
        new = {"renamed.txt": old["a.txt"]}
        current = old.copy()
    merged, conflicts = merge_snapshots(tmp_path, old, new, current)
    if case == "renamed":
        assert conflicts == []
        assert merged == new
    elif case == "disjoint":
        assert conflicts == []
        assert merged["a.txt"] == "new first\n" + middle + "my last\n"
    else:
        assert conflicts == ["a.txt"]


def test_init_deploy_bad_pristine_config_changes_nothing(invoke, tmp_path, monkeypatch):
    assert invoke("init", "p", "--name", "demo", "--users", "3", "--source", "demo").exit_code == 0
    root = tmp_path / "p"
    p = root / BASELINE
    baseline = json.loads(p.read_text())
    baseline["files"]["vecten.toml"] = "["
    p.write_text(json.dumps(baseline))
    before = files(root)
    monkeypatch.chdir(root)
    result = invoke(
        "init",
        "--deploy",
        "--cloud",
        "aws",
        "--domain",
        "data.example.com",
        "--dns-zone",
        "example.com",
        "--github-repository",
        "example/demo",
        "--json",
    )
    assert result.exit_code == 6, result.stdout
    assert files(root) == before


@pytest.mark.parametrize("owner_edited", [False, True])
def test_update_handles_file_removed_by_new_release(invoke, tmp_path, monkeypatch, owner_edited):
    root = project(tmp_path, monkeypatch)
    name = "docs/retired.md"
    original = "Instructions from the old release.\n"
    baseline_path = root / BASELINE
    baseline = json.loads(baseline_path.read_text())
    baseline["files"][name] = original
    baseline_path.write_text(json.dumps(baseline))
    (root / name).write_text("Owner instructions.\n" if owner_edited else original)
    before = files(root)

    result = invoke("update", "--json")
    doc = json.loads(result.stdout)
    if owner_edited:
        assert result.exit_code == 4, result.stdout
        assert doc["conflicts"] == [name]
        assert files(root) == before
    else:
        assert result.exit_code == 0, result.stdout
        assert name in doc["changed"]
        assert not (root / name).exists()
        assert name not in json.loads(baseline_path.read_text())["files"]
        assert load_config(root).project.scaffold_version == VERSIONS.vecten


def test_update_keeps_a_project_whose_sources_are_free_text(invoke, tmp_path, monkeypatch):
    old = replace(VERSIONS, vecten="0.0.9", terraform_module_ref="v0.0.9")
    monkeypatch.setattr(params_module, "VERSIONS", old)
    render_project(tmp_path, ScaffoldParams(project_name="demo", users=3, sources=["our CRM"]))
    monkeypatch.setattr(params_module, "VERSIONS", VERSIONS)
    monkeypatch.chdir(tmp_path)
    result = invoke("update", "--json")
    assert result.exit_code == 0, result.stdout
