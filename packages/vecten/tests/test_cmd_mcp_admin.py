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


def _areas_project(invoke, workdir, groups=("finance",)):
    from vecten_shared.config import (
        AccessDecl,
        AreaDecl,
        EnvironmentFacts,
        load_config,
        save_config,
    )

    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    config = load_config(workdir)
    config.areas = {"finance": AreaDecl(access=AccessDecl(groups=list(groups)))}
    config.environments["prod"] = EnvironmentFacts(deployed_commit="a" * 40)
    save_config(workdir, config)


def _client(monkeypatch, grants):
    calls = []

    class Client:
        def __init__(self, root, env):
            pass

        def call(self, operation, data=None):
            calls.append((operation, data))
            if operation == "grants":
                return grants
            return dict(data or {})

    monkeypatch.setattr("vecten.mcp_client.AdminClient", Client)
    return calls


def test_grant_and_revoke_take_an_area(invoke, workdir, monkeypatch):
    _areas_project(invoke, workdir)
    calls = _client(monkeypatch, [])
    result = invoke("grant", "--area", "finance", "--group", "finance", "--json")
    assert result.exit_code == 0, result.output
    assert ("grant", {"target": "finance", "kind": "area", "group": "finance"}) in calls
    result = invoke("revoke", "--area", "finance", "--group", "finance", "--json")
    assert result.exit_code == 0, result.output
    assert ("revoke", {"target": "finance", "kind": "area", "group": "finance"}) in calls


def test_grant_names_exactly_one_of_a_dataset_a_schema_and_an_area(invoke, workdir, monkeypatch):
    _areas_project(invoke, workdir)
    calls = _client(monkeypatch, [])
    for extra in (["orders", "--area", "finance"], ["--schema", "core", "--area", "finance"]):
        result = invoke("grant", *extra, "--group", "finance", "--json")
        assert result.exit_code == 2, result.output
        assert "--area" in json.loads(result.output)["error"]
    assert calls == []


def test_an_area_grant_records_the_declared_access_once_every_group_holds_it(
    invoke, workdir, monkeypatch
):
    from vecten_shared.config import load_config

    _areas_project(invoke, workdir, groups=("finance", "board"))
    held = [{"group": "finance", "kind": "area", "target": "finance"}]
    _client(monkeypatch, held)
    assert invoke("grant", "--area", "finance", "--group", "finance").exit_code == 0
    assert load_config(workdir).environments["prod"].area_access_applied == []
    held.append({"group": "board", "kind": "area", "target": "finance"})
    assert invoke("grant", "--area", "finance", "--group", "board").exit_code == 0
    assert load_config(workdir).environments["prod"].area_access_applied == ["finance"]


def test_a_grant_the_warehouse_applied_only_in_part_names_the_refusals_and_fails(
    invoke, workdir, monkeypatch
):
    _areas_project(invoke, workdir)
    refusal = "marts_finance: FUTURE ICEBERG TABLES: unsupported feature 'ICEBERG'"

    class Client:
        def __init__(self, root, env):
            pass

        def call(self, operation, data=None):
            if operation == "grants":
                return []
            return dict(data or {}, refused=[refusal])

    monkeypatch.setattr("vecten.mcp_client.AdminClient", Client)
    result = invoke("grant", "--area", "finance", "--group", "finance", "--json")
    assert result.exit_code == 5, result.output
    doc = json.loads(result.output)
    assert refusal in doc["error"] and doc["refused"] == [refusal]
    result = invoke("grant", "--area", "finance", "--group", "finance")
    assert result.exit_code == 5 and "ICEBERG" in result.output


def test_group_add_names_the_role_and_bind_prints_then_confirms(invoke, workdir, monkeypatch):
    assert (
        invoke("init", ".", "--name", "demo", "--users", "5", "--source", "hubspot").exit_code == 0
    )
    calls = []
    statement = 'GRANT ROLE "FINANCE_ANALYST" TO ROLE "FIN_READERS";'

    class Client:
        def __init__(self, root, env):
            pass

        def call(self, operation, data=None):
            calls.append((operation, data))
            if operation == "groups":
                return []
            if operation in {"groups/bind", "groups/confirm"}:
                return {
                    "group": "finance",
                    "bound_role": "finance_analyst",
                    "confirmed": operation == "groups/confirm",
                    "statement": statement,
                    "review": 'SHOW GRANTS TO ROLE "FINANCE_ANALYST";',
                }
            if operation == "groups/unbind":
                return {"group": "finance", "statement": "REVOKE ROLE x FROM ROLE y;"}
            return dict(data or {})

    monkeypatch.setattr("vecten.mcp_client.AdminClient", Client)
    result = invoke("group", "add", "finance", "--role", "fin_readers", "--json")
    assert result.exit_code == 0, result.output
    assert ("groups/create", {"name": "finance", "role": "fin_readers"}) in calls
    result = invoke("group", "bind", "finance", "finance_analyst")
    assert result.exit_code == 0, result.output
    assert statement in result.output and "--confirm" in result.output
    assert calls[-1] == ("groups/bind", {"group": "finance", "role": "finance_analyst"})
    result = invoke("group", "bind", "finance", "finance_analyst", "--confirm", "--json")
    assert result.exit_code == 0, result.output
    assert calls[-1] == ("groups/confirm", {"group": "finance"})
    result = invoke("group", "unbind", "finance")
    assert result.exit_code == 0 and "REVOKE ROLE x FROM ROLE y;" in result.output
    assert calls[-1] == ("groups/unbind", {"group": "finance"})
