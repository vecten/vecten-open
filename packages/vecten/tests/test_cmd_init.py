import json

import pytest
import yaml

from vecten.knowhow import load_skill, skill_names
from vecten.knowhow.mcp_servers import mcp_servers_document
from vecten_shared.config import load_config

LOCAL = ("--name", "acme", "--users", "8", "--source", "hubspot", "--source", "stripe")
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


def test_complete_intake_renders_with_deploy_layer(invoke, workdir):
    r = invoke("init", "my-project", *LOCAL, *DEPLOY, "--json")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["deploy_layer"] is True and doc["components"] == ["platform", "mcp"]
    proj = workdir / "my-project"
    cfg = load_config(proj)
    assert cfg.wanted == ["hubspot", "stripe"] and cfg.deploy.cloud == "aws"
    assert (proj / "deploy" / "envs" / "prod.tfvars").is_file()
    assert (proj / ".copier-answers.yml").is_file()


def test_size_from_users(invoke, workdir):
    invoke("init", "p", "--name", "acme", "--users", "30", "--source", "hubspot", "--json")
    assert load_config(workdir / "p").size == "medium"


def test_local_answers_only_renders_without_deploy(invoke, workdir):
    r = invoke("init", "p", *LOCAL, "--json")
    assert r.exit_code == 0
    assert json.loads(r.stdout)["deploy_layer"] is False
    assert not (workdir / "p" / "deploy").exists()
    workflows = workdir / "p" / ".github" / "workflows"
    assert sorted(p.name for p in workflows.iterdir()) == ["ci.yml"]


def test_missing_users_named_and_nothing_rendered(invoke, workdir):
    r = invoke("init", "p", "--name", "acme", "--source", "hubspot", "--json")
    assert r.exit_code == 2
    assert "users" in json.loads(r.stdout)["error"]
    assert not (workdir / "p").exists()


def test_answers_file(invoke, workdir):
    (workdir / "answers.yml").write_text(
        yaml.safe_dump(
            {
                "name": "acme",
                "users": 4,
                "sources": ["hubspot"],
                "cloud": "scaleway",
                "domain": "d.acme.com",
                "dns_zone": "acme.com",
                "github_repository": "acme/d",
            }
        )
    )
    r = invoke("init", "p", "--answers-file", "answers.yml", "--json")
    assert r.exit_code == 0, r.stdout
    assert load_config(workdir / "p").deploy.region == "fr-par"


def test_init_reads_recorded_answers(invoke, workdir):
    for k, v in (("name", "acme"), ("users", "4"), ("sources", "hubspot")):
        invoke("answer", k, v)
    r = invoke("init", "--json")
    assert r.exit_code == 0, r.stdout
    assert load_config(workdir).wanted == ["hubspot"]
    assert (workdir / "vecten-answers.yml").is_file()


def test_init_renders_locally_after_a_declined_cloud(invoke, workdir):
    for k, v in (("name", "acme"), ("users", "4"), ("sources", "hubspot"), ("cloud", "none")):
        assert invoke("answer", k, v).exit_code == 0
    r = invoke("init", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["deploy_layer"] is False
    assert load_config(workdir).deploy is None


def test_existing_warehouse_mcp_only(invoke, workdir):
    r = invoke(
        "init",
        "p",
        "--name",
        "acme",
        "--users",
        "4",
        "--has-warehouse",
        "yes",
        "--warehouse-url",
        "postgres://host:5432/dw",
        "--json",
    )
    assert r.exit_code == 0, r.stdout
    cfg = load_config(workdir / "p")
    assert cfg.components == ["mcp"] and cfg.warehouse.existing and cfg.warehouse.host == "host"
    assert not (workdir / "p" / "connectors").exists()


def test_init_over_an_existing_warehouse_states_the_served_schemas(invoke, workdir):
    r = invoke(
        "init",
        "p",
        "--name",
        "acme",
        "--users",
        "4",
        "--has-warehouse",
        "yes",
        "--warehouse-url",
        "postgres://host:5432/dw",
    )
    assert r.exit_code == 0, r.stdout
    assert "the MCP serves the core and marts schemas of dw" in r.stdout
    r = invoke("init", "q", "--name", "acme", "--users", "4", "--source", "hubspot")
    assert r.exit_code == 0, r.stdout
    assert "core and marts" not in r.stdout


def test_password_in_url_refused(invoke, workdir):
    r = invoke(
        "init",
        "p",
        "--name",
        "acme",
        "--users",
        "4",
        "--has-warehouse",
        "yes",
        "--warehouse-url",
        "postgres://admin:secret@host/dw",
        "--json",
    )
    assert r.exit_code == 2 and "secret set" in json.loads(r.stdout)["error"]
    assert not (workdir / "p").exists()


def test_source_with_existing_warehouse_refused(invoke, workdir):
    r = invoke(
        "init",
        "p",
        "--name",
        "acme",
        "--users",
        "4",
        "--has-warehouse",
        "yes",
        "--warehouse-url",
        "postgres://host/dw",
        "--source",
        "hubspot",
        "--json",
    )
    assert r.exit_code == 2 and "platform" in json.loads(r.stdout)["error"]
    assert not (workdir / "p").exists()


def test_minimal_profile_mcp_only(invoke, workdir):
    r = invoke(
        "init",
        "p",
        "--name",
        "acme",
        "--users",
        "4",
        "--source",
        "hubspot",
        "--components",
        "mcp",
        "--profile",
        "minimal",
        *DEPLOY,
        "--json",
    )
    assert r.exit_code == 0, r.stdout
    text = (workdir / "p" / "deploy" / "envs" / "prod.tfvars").read_text()
    assert 'profile           = "minimal"' in text and 'components        = ["mcp"]' in text


def test_unsupported_cloud_and_warehouse(invoke):
    r = invoke("init", "p", *LOCAL, "--cloud", "digitalocean", "--json")
    assert r.exit_code == 2
    error = json.loads(r.stdout)["error"]
    assert "digitalocean" in error and "aws" in error and "scaleway" in error
    r = invoke("init", "p", *LOCAL, "--warehouse", "bigquery", "--json")
    assert r.exit_code == 2
    error = json.loads(r.stdout)["error"]
    assert "bigquery" in error and "postgres" in error and "snowflake" in error


def test_non_numeric_users_refused_as_json(invoke, workdir):
    r = invoke("init", "p", "--name", "acme", "--users", "abc", "--source", "hubspot", "--json")
    assert r.exit_code == 2
    assert "users" in json.loads(r.stdout)["error"]
    assert not (workdir / "p").exists()


def test_failed_render_removes_directory_init_created(invoke, workdir, monkeypatch):
    def broken_render(target, params):
        target.mkdir(parents=True)
        (target / "partial.txt").write_text("x")
        raise RuntimeError("copier failed")

    monkeypatch.setattr("vecten.cli.commands.init.render_project", broken_render)
    r = invoke("init", "p", *LOCAL, "--json")
    assert r.exit_code == 1
    assert not (workdir / "p").exists()


def test_failed_render_keeps_directory_that_existed(invoke, workdir, monkeypatch):
    def broken_render(target, params):
        raise RuntimeError("copier failed")

    (workdir / "p").mkdir()
    monkeypatch.setattr("vecten.cli.commands.init.render_project", broken_render)
    r = invoke("init", "p", *LOCAL, "--json")
    assert r.exit_code == 1
    assert (workdir / "p").is_dir()


def test_init_refuses_non_empty_target(invoke, workdir):
    (workdir / "p").mkdir()
    (workdir / "p" / "notes.txt").write_text("x")
    r = invoke("init", "p", *LOCAL, "--json")
    assert r.exit_code == 4 and "p" in json.loads(r.stdout)["error"]
    assert (workdir / "p" / "notes.txt").read_text() == "x"
    assert not (workdir / "p" / "vecten.toml").exists()


@pytest.mark.parametrize(
    "args",
    [
        LOCAL,
        (
            "--name",
            "acme",
            "--users",
            "3",
            "--has-warehouse",
            "yes",
            "--warehouse-url",
            "postgres://dw:5432/a",
        ),
    ],
    ids=["platform", "mcp-only"],
)
def test_init_vendors_the_skills_and_mcp_json(invoke, workdir, args):
    r = invoke("init", "p", *args, "--json")
    assert r.exit_code == 0, r.stdout
    proj = workdir / "p"
    for name in skill_names():
        assert (proj / "skills" / name / "SKILL.md").read_text(encoding="utf-8") == load_skill(name)
    if "--source" in args:
        mcp = json.loads((proj / ".mcp.json").read_text(encoding="utf-8"))
        assert mcp == mcp_servers_document()
    else:
        assert not (proj / ".mcp.json").exists()


@pytest.mark.parametrize("components", ["platform,mcp", "mcp"])
@pytest.mark.parametrize("json_output", [False, True])
def test_init_counts_each_rendered_file_once(invoke, workdir, components, json_output):
    flags = ["--json"] if json_output else []
    result = invoke("init", "p", *LOCAL, "--components", components, *flags)
    assert result.exit_code == 0, result.stdout
    root = workdir / "p"
    count = sum(p.is_file() for p in root.rglob("*") if ".vecten" not in p.relative_to(root).parts)
    record = json.loads((root / ".vecten/records/init.json").read_text())
    assert record["files"] == count
    if json_output:
        assert json.loads(result.stdout)["files"] == count
    else:
        assert f"rendered {count} files into" in result.stdout


def test_snowflake_account_renders_into_tfvars_and_vecten_toml(invoke, workdir):
    from vecten.deploy.tfvars import check_tfvars

    flags = ("--warehouse", "snowflake", "--snowflake-account", "ACME-PROD")
    r = invoke("init", "p", *LOCAL, *flags, *DEPLOY, "--json")
    assert r.exit_code == 0, r.stdout
    proj = workdir / "p"
    config = load_config(proj)
    assert config.warehouse.account == "ACME-PROD"
    assert 'snowflake_account = "ACME-PROD"' in (proj / "deploy/envs/prod.tfvars").read_text()
    assert check_tfvars(proj, config, "prod")["snowflake_account"] == "ACME-PROD"


@pytest.mark.parametrize(
    ("warehouse", "account"), [("snowflake", "xy12345.eu-central-1"), ("postgres", "ACME-PROD")]
)
def test_a_wrong_snowflake_account_is_usage_and_renders_nothing(
    invoke, workdir, warehouse, account
):
    flags = ("--warehouse", warehouse, "--snowflake-account", account)
    r = invoke("init", "p", *LOCAL, *flags, *DEPLOY, "--json")
    assert r.exit_code == 2, r.stdout
    assert not (workdir / "p").exists()


def test_init_refuses_a_free_text_source(invoke, workdir):
    r = invoke("init", "proj", "--name", "acme", "--users", "3", "--source", "our CRM")
    assert r.exit_code == 2
    assert "our CRM" in r.stdout + r.stderr
    assert not (workdir / "proj").exists()


def test_init_lowercases_source_names(invoke, workdir):
    r = invoke("init", "proj", "--name", "acme", "--users", "3", "--source", "HubSpot", "--json")
    assert r.exit_code == 0, r.stdout
    assert load_config(workdir / "proj").wanted == ["hubspot"]


def test_init_refuses_a_free_text_source_from_the_answers_file(invoke, workdir):
    (workdir / "vecten-answers.yml").write_text(
        yaml.safe_dump({"name": "acme", "users": 3, "sources": ["our CRM"]})
    )
    r = invoke("init", "proj", "--answers-file", "vecten-answers.yml")
    assert r.exit_code == 2
    assert not (workdir / "proj").exists()
