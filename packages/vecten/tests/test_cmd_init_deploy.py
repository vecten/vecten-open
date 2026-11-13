import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import load_config, save_config

LOCAL = ("--name", "acme", "--users", "8", "--source", "hubspot")
DEPLOY = (
    "--cloud",
    "aws",
    "--domain",
    "data.acme.com",
    "--dns-zone",
    "acme.com",
    "--github-repository",
    "acme/data",
)


def digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and ".vecten" not in p.parts
    }


def test_deploy_layer_after_laptop_loop(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    proj = workdir / "p"
    (proj / "connectors" / "hubspot.py").write_text("# user file\n")
    before = digest(proj)
    assert {"skills/guide/SKILL.md", ".mcp.json"} <= set(before)
    monkeypatch.chdir(proj)
    for k, v in (
        ("cloud", "aws"),
        ("domain", "data.acme.com"),
        ("dns_zone", "acme.com"),
        ("github_repository", "acme/data"),
    ):
        invoke("answer", k, v)
    r = invoke("init", "--deploy", "--json")
    assert r.exit_code == 0, r.stdout
    after = digest(proj)
    assert (proj / "deploy" / "terraform" / "aws" / "main.tf").is_file()
    assert "<" not in (proj / "deploy" / "envs" / "prod.tfvars").read_text()
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {
        "vecten.toml",
        ".copier-answers.yml",
        ".copier-baseline.json",
        "deploy/envs/prod.tfvars",
        "deploy/terraform/aws/main.tf",
        "deploy/terraform/aws/variables.tf",
        "deploy/terraform/aws/backend.tf",
        "deploy/terraform/aws/outputs.tf",
        ".github/workflows/terraform-plan.yml",
        ".github/workflows/ship.yml",
        ".github/workflows/terraform-apply.yml",
        ".github/workflows/deploy-platform.yml",
        "docs/runbook.md",
        "vecten-answers.yml",
    }
    assert (proj / "connectors" / "hubspot.py").read_text() == "# user file\n"
    assert load_config(proj).deploy.cloud == "aws"
    runbook = (proj / "docs" / "runbook.md").read_text()
    assert "## AWS deployment" in runbook and "### Restore the warehouse" in runbook
    tunnel_psql = [line for line in runbook.splitlines() if "port=15432" in line]
    assert tunnel_psql
    assert all("gssencmode=disable" in line for line in tunnel_psql), tunnel_psql


def test_runbook_keeps_the_humans_edits_and_gains_the_cloud_sections(invoke, workdir, monkeypatch):
    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    root = workdir / "p"
    runbook = root / "docs" / "runbook.md"
    runbook.write_text(runbook.read_text().replace("# Runbook\n", "# Runbook\n\nOn call: Ola.\n"))
    monkeypatch.chdir(root)
    r = invoke("init", "--deploy", *DEPLOY, "--json")
    assert r.exit_code == 0, r.stdout
    assert "docs/runbook.md" in json.loads(r.stdout)["changed"]
    text = runbook.read_text()
    assert "On call: Ola." in text and "## AWS deployment" in text
    pristine = json.loads((root / ".copier-baseline.json").read_text())["files"]
    assert "## AWS deployment" in pristine["docs/runbook.md"]
    assert "On call: Ola." not in pristine["docs/runbook.md"]


def test_conflicting_runbook_edit_is_kept_and_named_for_a_hand_merge(invoke, workdir, monkeypatch):
    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    root = workdir / "p"
    runbook = root / "docs" / "runbook.md"
    pristine = runbook.read_text()
    # The cloud sections render right after the dbt section's last line.
    last = "Postgres.\n"
    assert pristine.count(last) == 1
    edited = pristine.replace(last, "Postgres.\n\n## Our own deployment notes\n")
    runbook.write_text(edited)
    monkeypatch.chdir(root)
    r = invoke("init", "--deploy", *DEPLOY, "--json")
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["hand_merge"] == ["docs/runbook.md"]
    assert "docs/runbook.md" not in out["changed"]
    assert (root / "deploy" / "terraform" / "aws" / "main.tf").is_file()
    assert runbook.read_text() == edited
    baseline = json.loads((root / ".copier-baseline.json").read_text())["files"]
    assert baseline["docs/runbook.md"] == pristine


def test_missing_domain_named_and_nothing_rendered(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke(
        "init",
        "--deploy",
        "--cloud",
        "aws",
        "--dns-zone",
        "acme.com",
        "--github-repository",
        "acme/data",
        "--json",
    )
    assert r.exit_code == 2 and "domain" in json.loads(r.stdout)["error"]
    assert not (workdir / "p" / "deploy").exists()


def test_declined_deploy_answers_count_as_missing(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    for key in ("cloud", "domain", "github_repository"):
        assert invoke("answer", key, "none").exit_code == 0
    assert invoke("answer", "dns_zone", "acme.com").exit_code == 0
    r = invoke("init", "--deploy", "--json")
    assert r.exit_code == 2, r.stdout
    doc = json.loads(r.stdout)
    assert doc["missing"] == ["cloud", "domain", "github_repository"]
    assert not (workdir / "p" / "deploy").exists()


def test_run_again_changes_nothing(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    invoke("init", "--deploy", *DEPLOY, "--json")
    before = digest(workdir / "p")
    r = invoke("init", "--deploy", *DEPLOY, "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["changed"] == []
    assert digest(workdir / "p") == before


def test_cloud_change_refused(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, *DEPLOY, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("init", "--deploy", "--cloud", "scaleway", "--json")
    assert r.exit_code == 4 and "cloud" in json.loads(r.stdout)["error"]
    assert (workdir / "p" / "deploy" / "terraform" / "aws").is_dir()


def test_outside_project_refused(invoke):
    r = invoke("init", "--deploy", *DEPLOY, "--json")
    assert r.exit_code == 4


def test_cloud_change_allowed_before_a_layer_is_rendered(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--cloud", "aws", "--json")
    proj = workdir / "p"
    assert not (proj / "deploy").exists()
    monkeypatch.chdir(proj)
    r = invoke(
        "init",
        "--deploy",
        "--cloud",
        "scaleway",
        "--domain",
        "d.acme.com",
        "--dns-zone",
        "acme.com",
        "--github-repository",
        "acme/d",
        "--json",
    )
    assert r.exit_code == 0, r.stdout
    assert (proj / "deploy" / "terraform" / "scaleway").is_dir()
    assert load_config(proj).deploy.cloud == "scaleway"
    assert load_config(proj).deploy.region == "fr-par"


def test_unknown_cloud_is_usage_not_refused(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, *DEPLOY, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("init", "--deploy", "--cloud", "gcp", "--json")
    assert r.exit_code == 2 and "aws, scaleway" in json.loads(r.stdout)["error"]


def test_non_deploy_flag_refused(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("init", "--deploy", *DEPLOY, "--profile", "minimal", "--json")
    error = json.loads(r.stdout)["error"]
    assert r.exit_code == 2 and "--profile" in error and "without --deploy" in error
    assert not (workdir / "p" / "deploy").exists()


def test_answers_file_replaces_recorded_answers(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    invoke("answer", "domain", "data.acme.com")
    (proj / "deploy-answers.yml").write_text(
        "cloud: aws\ndns_zone: acme.com\ngithub_repository: acme/data\n"
    )
    r = invoke("init", "--deploy", "--answers-file", "deploy-answers.yml", "--json")
    assert r.exit_code == 2 and "domain" in json.loads(r.stdout)["error"]
    assert not (proj / "deploy").exists()


def test_rerun_without_flags_uses_recorded_answers(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, *DEPLOY, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("init", "--deploy", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["changed"] == []


def test_profile_from_vecten_toml_reaches_tfvars(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, *DEPLOY, "--json")
    proj = workdir / "p"
    toml = proj / "vecten.toml"
    toml.write_text(toml.read_text().replace('profile = "standard"', 'profile = "minimal"'))
    monkeypatch.chdir(proj)
    r = invoke("init", "--deploy", "--json")
    assert r.exit_code == 0, r.stdout
    assert '"minimal"' in (proj / "deploy" / "envs" / "prod.tfvars").read_text()
    assert "deploy/envs/prod.tfvars" in json.loads(r.stdout)["changed"]


@pytest.mark.parametrize("failure", ["answers", "config"])
def test_failed_deploy_write_does_not_advance_baseline(invoke, workdir, monkeypatch, failure):
    from vecten.cli.commands import init_deploy

    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    root = workdir / "p"
    baseline = root / ".copier-baseline.json"
    before = baseline.read_bytes()
    monkeypatch.chdir(root)
    write = init_deploy.write_text_atomic

    def fail_answers(path, text):
        if path.name == ".copier-answers.yml":
            raise OSError("simulated answers write failure")
        write(path, text)

    def fail_config(*args):
        raise OSError("simulated config write failure")

    with monkeypatch.context() as patch:
        if failure == "answers":
            patch.setattr(init_deploy, "write_text_atomic", fail_answers)
        else:
            patch.setattr(init_deploy, "save_config", fail_config)
        result = invoke("init", "--deploy", *DEPLOY, "--json")
    assert result.exit_code != 0, result.stdout
    assert baseline.read_bytes() == before
    assert load_config(root).deploy is None

    retry = invoke("init", "--deploy", *DEPLOY, "--json")
    assert retry.exit_code == 0, retry.stdout
    pristine = json.loads(baseline.read_text())["files"]
    assert (
        tomllib.loads(pristine["vecten.toml"])["deploy"]
        == tomllib.loads((root / "vecten.toml").read_text())["deploy"]
    )


def test_a_recorded_snowflake_account_reaches_tfvars_and_vecten_toml(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--warehouse", "snowflake", *DEPLOY, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    assert load_config(proj).warehouse.account is None
    invoke("answer", "snowflake_account", "ACME-PROD")
    r = invoke("init", "--deploy", "--json")
    assert r.exit_code == 0, r.stdout
    assert {"vecten.toml", "deploy/envs/prod.tfvars"} <= set(json.loads(r.stdout)["changed"])
    assert load_config(proj).warehouse.account == "ACME-PROD"
    assert 'snowflake_account = "ACME-PROD"' in (proj / "deploy/envs/prod.tfvars").read_text()
    again = invoke("init", "--deploy", "--json")
    assert json.loads(again.stdout)["changed"] == []


@pytest.mark.parametrize(("answer", "shared"), [("yes", True), ("no", False)])
def test_the_shared_account_answer_reaches_vecten_toml(
    invoke, workdir, monkeypatch, answer, shared
):
    invoke("init", "p", *LOCAL, "--warehouse", "snowflake", *DEPLOY, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    invoke("answer", "snowflake_account", "ACME-PROD")
    invoke("answer", "snowflake_shared_account", answer)
    r = invoke("init", "--deploy", "--json")
    assert r.exit_code == 0, r.stdout
    assert load_config(proj).warehouse.shared_account is shared
    assert ("shared_account = true" in (proj / "vecten.toml").read_text()) is shared
    again = invoke("init", "--deploy", "--json")
    assert json.loads(again.stdout)["changed"] == []


def test_the_shared_account_answer_leaves_a_deployed_project_as_it_is(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--warehouse", "snowflake", *DEPLOY, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    invoke("answer", "snowflake_account", "ACME-PROD")
    assert invoke("init", "--deploy", "--json").exit_code == 0
    config = load_config(proj)
    config.environments["prod"].snowflake_shared_account = False
    save_config(proj, config)
    invoke("answer", "snowflake_shared_account", "yes")
    r = invoke("init", "--deploy", "--json")
    assert r.exit_code == 0, r.stdout
    assert load_config(proj).warehouse.shared_account is False


def test_snowflake_account_flag_is_a_deploy_flag(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--warehouse", "snowflake", *DEPLOY, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    r = invoke("init", "--deploy", "--snowflake-account", "ACME-PROD", "--json")
    assert r.exit_code == 0, r.stdout
    assert load_config(proj).warehouse.account == "ACME-PROD"


def test_init_deploy_keeps_a_project_whose_sources_are_free_text(invoke, workdir, monkeypatch):
    proj = workdir / "p"
    render_project(proj, ScaffoldParams(project_name="acme", users=3, sources=["our CRM"]))
    monkeypatch.chdir(proj)
    r = invoke("init", "--deploy", *DEPLOY, "--json")
    assert r.exit_code == 0, r.stdout
