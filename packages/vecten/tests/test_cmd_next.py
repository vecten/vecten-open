import errno
import json
from pathlib import Path

from vecten.next.assemble import next_items
from vecten_shared.config import load_config, save_config

from .fixture_project import render_fixture
from .test_next_assemble import clear_local_loop, clear_mcp_only_local_loop
from .test_next_project import render_mcp_only


def test_json_prints_one_document_with_items(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    result = invoke("next", "--json")
    assert result.exit_code == 0, result.output
    assert result.stdout.count("\n") == 1
    doc = json.loads(result.stdout)
    assert doc["items"] == [item.to_json() for item in next_items(root)]
    assert doc["items"][0] == {
        "kind": "command",
        "title": "Start the local stack",
        "resolution": "vecten up",
    }


def test_text_prints_one_line_per_item(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres")
    monkeypatch.chdir(root)
    result = invoke("next")
    assert result.exit_code == 0, result.stderr
    expected = [f"{i.kind}: {i.title} -> {i.resolution}" for i in next_items(root)]
    assert result.stdout.splitlines() == expected
    assert expected[0] == "command: Start the local stack -> vecten up"


def test_outside_a_project_reports_the_intake(invoke, tmp_path):
    result = invoke("next", "--json")
    assert result.exit_code == 0, result.output
    items = json.loads(result.stdout)["items"]
    assert [i["answer_keys"] for i in items] == [["has_warehouse"]]


def test_nothing_left_exits_zero(invoke, tmp_path, monkeypatch):
    root = render_fixture(tmp_path, "postgres", deploy=True)
    clear_local_loop(root)
    config = load_config(root)
    config.mcp.auth = "password"
    config.mcp.admins = ["ana@acme.com"]
    facts = config.environments["prod"]
    facts.bootstrapped = True
    facts.deployed_commit = "abc123"
    facts.first_user_added = True
    facts.connector_access_applied = ["demo"]
    facts.connection_shown = True
    facts.alert_webhook_asked = True
    save_config(root, config)
    monkeypatch.chdir(root)
    result = invoke("next", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"items": [], "ok": True}
    result = invoke("next")
    assert result.exit_code == 0
    assert result.stdout == "nothing left to do\n"


def deny_env(monkeypatch):
    real_read, real_is_file = Path.read_text, Path.is_file

    def read_text(self, *args, **kwargs):
        if self.name == ".env":
            raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(
        Path, "is_file", lambda self: False if self.name == ".env" else real_is_file(self)
    )


def test_next_exits_three_on_an_unreadable_env(invoke, tmp_path, monkeypatch):
    root = render_mcp_only(tmp_path)
    clear_mcp_only_local_loop(root, tmp_path)
    monkeypatch.chdir(root)
    deny_env(monkeypatch)
    result = invoke("next", "--json")
    assert result.exit_code == 3, result.output
    assert ".env" in result.output
