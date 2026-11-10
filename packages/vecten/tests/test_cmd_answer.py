import json
import tomllib

import yaml

from vecten.intake.answers import SOURCES_NEED_PLATFORM
from vecten.next.assemble import next_items
from vecten.next.items import Item
from vecten.scaffold.params import ScaffoldParams
from vecten.scaffold.render import render_project
from vecten_shared.config import ConnectorDecl, load_config, save_config

from .fixture_project import render_fixture


def test_answer_records_value(invoke, workdir):
    r = invoke("answer", "cloud", "aws", "--json")
    assert r.exit_code == 0
    assert json.loads(r.stdout) == {
        "ok": True,
        "key": "cloud",
        "value": "aws",
        "file": "vecten-answers.yml",
    }
    assert yaml.safe_load((workdir / "vecten-answers.yml").read_text()) == {"cloud": "aws"}


def test_answer_inside_a_project_writes_at_the_project_root(invoke, workdir, monkeypatch):
    init = invoke("init", "p", "--name", "acme", "--users", "8", "--source", "hubspot", "--json")
    assert init.exit_code == 0, init.stdout
    sub = workdir / "p" / "docs"
    monkeypatch.chdir(sub)
    assert invoke("answer", "cloud", "aws", "--json").exit_code == 0
    assert yaml.safe_load((workdir / "p" / "vecten-answers.yml").read_text()) == {"cloud": "aws"}
    assert not (sub / "vecten-answers.yml").exists()


def test_answer_invalid_exits_two(invoke, workdir):
    r = invoke("answer", "cloud", "digitalocean", "--json")
    assert r.exit_code == 2
    doc = json.loads(r.stdout)
    assert doc["ok"] is False and "scaleway" in doc["error"]
    assert not (workdir / "vecten-answers.yml").exists()


def _project_with_connector(invoke, workdir, monkeypatch):
    init = invoke("init", "p", "--name", "acme", "--users", "3", "--source", "hubspot", "--json")
    assert init.exit_code == 0, init.stdout
    root = workdir / "p"
    config = load_config(root)
    config.connectors.append(ConnectorDecl(name="hubspot", source="dlt"))
    save_config(root, config)
    monkeypatch.chdir(root)
    return root


def test_access_answer_lands_on_the_declaration(invoke, workdir, monkeypatch):
    root = _project_with_connector(invoke, workdir, monkeypatch)
    r = invoke("answer", "access.hubspot", "sales, marketing", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["value"] == ["marketing", "sales"]
    decl = tomllib.loads((root / "vecten.toml").read_text())["connectors"][0]
    assert decl["access"] == {"groups": ["marketing", "sales"]}
    assert not (root / "vecten-answers.yml").exists()


def test_access_answer_for_unknown_connector_is_usage(invoke, workdir, monkeypatch):
    _project_with_connector(invoke, workdir, monkeypatch)
    r = invoke("answer", "access.stripe", "sales", "--json")
    assert r.exit_code == 2 and "stripe" in json.loads(r.stdout)["error"]


def test_access_answer_validates_group_names(invoke, workdir, monkeypatch):
    _project_with_connector(invoke, workdir, monkeypatch)
    r = invoke("answer", "access.hubspot", "Sales Team", "--json")
    assert r.exit_code == 2 and "lowercase" in json.loads(r.stdout)["error"]


def test_access_answer_again_replaces_the_groups(invoke, workdir, monkeypatch):
    root = _project_with_connector(invoke, workdir, monkeypatch)
    assert invoke("answer", "access.hubspot", "sales, marketing", "--json").exit_code == 0
    assert invoke("answer", "access.hubspot", "finance", "--json").exit_code == 0
    decl = tomllib.loads((root / "vecten.toml").read_text())["connectors"][0]
    assert decl["access"] == {"groups": ["finance"]}


def test_access_answer_rejects_a_hyphenated_group(invoke, workdir, monkeypatch):
    root = _project_with_connector(invoke, workdir, monkeypatch)
    r = invoke("answer", "access.hubspot", "sales-ops", "--json")
    assert r.exit_code == 2 and "sales-ops" in json.loads(r.stdout)["error"]
    assert "access" not in tomllib.loads((root / "vecten.toml").read_text())["connectors"][0]


def test_access_answer_outside_a_project_is_refused(invoke):
    r = invoke("answer", "access.hubspot", "sales", "--json")
    assert r.exit_code == 4


def test_none_is_a_valid_deploy_answer(invoke, tmp_path):
    for key in ("cloud", "domain", "github_repository"):
        result = invoke("answer", key, "none", "--json")
        assert result.exit_code == 0, result.output
    answers = yaml.safe_load((tmp_path / "vecten-answers.yml").read_text())
    assert answers == {"cloud": "none", "domain": "none", "github_repository": "none"}


def test_mcp_keys_write_vecten_toml(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    assert invoke("answer", "mcp.auth", "password").exit_code == 0
    assert invoke("answer", "mcp.oidc_issuer", "https://login.example.com").exit_code == 0
    assert invoke("answer", "mcp.admins", "jane@acme.com, ola@acme.com").exit_code == 0
    assert invoke("answer", "alert_webhook.prod", "no").exit_code == 0
    cfg = load_config(root)
    assert cfg.mcp.auth == "password" and cfg.mcp.oidc_issuer == "https://login.example.com"
    assert cfg.mcp.admins == ["jane@acme.com", "ola@acme.com"]
    assert cfg.environments["prod"].alert_webhook_asked is True


def test_mcp_keys_refused_outside_a_project_and_validated(invoke, tmp_path, monkeypatch):
    assert invoke("answer", "mcp.auth", "password").exit_code == 4
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    assert invoke("answer", "mcp.auth", "ldap").exit_code == 2
    assert invoke("answer", "mcp.oidc_issuer", "http://insecure").exit_code == 2
    assert invoke("answer", "mcp.admins", "not-an-email").exit_code == 2
    assert invoke("answer", "alert_webhook.prod", "maybe").exit_code == 2


def test_mcp_answer_json_names_vecten_toml(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    r = invoke("answer", "mcp.oidc_client_id", " vecten-mcp ", "--json")
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout) == {
        "ok": True,
        "key": "mcp.oidc_client_id",
        "value": "vecten-mcp",
        "file": "vecten.toml",
    }
    assert invoke("answer", "mcp.oidc_client_id", "  ").exit_code == 2
    assert invoke("answer", "mcp.admins", "ola@acme.com").exit_code == 0
    assert load_config(root).mcp.admins == ["ola@acme.com"]
    assert not (root / "vecten-answers.yml").exists()


def test_alert_answer_names_a_valid_environment(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    r = invoke("answer", "alert_webhook.Prod", "yes", "--json")
    assert r.exit_code == 2, r.stdout
    assert "Prod" not in load_config(root).environments


def _platform_project(workdir, monkeypatch):
    root = workdir / "proj"
    answers = {"name": "acme", "users": 3, "sources": ["demo", "crm"]}
    render_project(root, ScaffoldParams.from_answers(answers, {}))
    monkeypatch.chdir(root)
    return root


def test_sources_inside_a_platform_project_correct_wanted(invoke, workdir, monkeypatch):
    root = _platform_project(workdir, monkeypatch)
    r = invoke("answer", "sources", "demo", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert load_config(root).wanted == ["demo"]
    assert yaml.safe_load((root / "vecten-answers.yml").read_text()) == {"sources": ["demo"]}
    items = next_items(root)
    assert Item("skill", "Connect demo", "connector") in items
    assert not any(i.title == "Connect crm" for i in items)


def test_sources_leave_out_declared_connectors(invoke, workdir, monkeypatch):
    root = _platform_project(workdir, monkeypatch)
    config = load_config(root)
    demo = ConnectorDecl(name="demo", source="custom", groups={"default": []})
    save_config(root, config.model_copy(update={"connectors": [demo]}))
    r = invoke("answer", "sources", "demo,billing", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert load_config(root).wanted == ["billing"]


def test_sources_inside_an_mcp_only_project_are_refused(invoke, workdir, monkeypatch):
    assert (
        invoke(
            "init",
            "proj",
            "--name",
            "acme",
            "--users",
            "4",
            "--has-warehouse",
            "yes",
            "--warehouse-url",
            "postgres://host:5432/dw",
            "--json",
        ).exit_code
        == 0
    )
    root = workdir / "proj"
    before = (root / "vecten.toml").read_bytes()
    monkeypatch.chdir(root)
    r = invoke("answer", "sources", "demo", "--json")
    assert r.exit_code == 2
    assert SOURCES_NEED_PLATFORM in r.stdout
    assert not (root / "vecten-answers.yml").exists()
    assert (root / "vecten.toml").read_bytes() == before


def test_free_text_sources_inside_a_project_change_nothing(invoke, workdir, monkeypatch):
    root = _platform_project(workdir, monkeypatch)
    before = (root / "vecten.toml").read_bytes()
    r = invoke("answer", "sources", "our CRM", "--json")
    assert r.exit_code == 2
    assert not (root / "vecten-answers.yml").exists()
    assert (root / "vecten.toml").read_bytes() == before
