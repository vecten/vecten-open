import json


def test_user_and_group_commands_use_admin_client(invoke, workdir, monkeypatch):
    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    calls = []

    class Client:
        def __init__(self, root, env):
            pass

        def call(self, operation, data=None):
            calls.append((operation, data))
            if operation == "groups":
                return []
            if operation == "grants":
                return []
            return {"id": "person", "warehouse_name": "jane"}

    monkeypatch.setattr("vecten.mcp_client.AdminClient", Client)
    result = invoke(
        "user", "add", "--email", "jane@example.com", "--name", "Jane", "--env", "local", "--json"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"]
    assert calls[0] == (
        "users/create",
        {"email": "jane@example.com", "name": "Jane", "adopt": False, "warehouse_name": None},
    )
    result = invoke("group", "add", "finance", "--env", "local", "--json")
    assert result.exit_code == 0, result.output
    assert ("groups/create", {"name": "finance"}) in calls
    result = invoke(
        "grant", "--schema", "raw_demo", "--group", "finance", "--env", "local", "--json"
    )
    assert result.exit_code == 0, result.output
    assert ("grant", {"target": "raw_demo", "kind": "schema", "group": "finance"}) in calls


def test_user_without_token_returns_json_login_guidance(invoke):
    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    result = invoke("user", "list", "--env", "local", "--json")
    assert result.exit_code == 3, result.output
    assert "vecten login" in json.loads(result.output)["error"]


def test_partial_reconciliation_is_not_reported_as_success(invoke, monkeypatch):
    invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot")
    partial = {"complete": False, "reconciled": 1, "failures": [{"error": "NotImplementedError"}]}

    class Client:
        def __init__(self, root, env):
            pass

        def call(self, operation, data=None):
            return partial if operation == "reconcile" else {"reconciliation": partial}

    monkeypatch.setattr("vecten.mcp_client.AdminClient", Client)
    result = invoke("reconcile", "--env", "local", "--json")
    assert result.exit_code == 5
    assert json.loads(result.output)["reconciled"] == 1
    result = invoke("revoke", "orders", "--group", "default", "--env", "local")
    assert result.exit_code == 5
    assert "revoked" in result.output
    assert "incomplete" in result.output
