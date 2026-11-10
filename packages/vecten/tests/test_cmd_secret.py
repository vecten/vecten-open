import errno
import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from vecten_shared import provider as provider_module
from vecten_shared.records import read_record

LOCAL = ("--name", "acme", "--users", "3", "--source", "hubspot")
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

STORE: dict[str, str] = {}
BOUND: list[str] = []


class FakeCloud:
    def __init__(self, project_root, env):
        self.project_root = project_root
        BOUND.append(env)

    def bootstrap(self, env, repository):
        return None

    def provision(self, env):
        return {}

    def destroy(self, env):
        return None

    def put_secret(self, name, value):
        STORE[name] = value

    def get_secret(self, name):
        return STORE[name]

    def list_secrets(self):
        return sorted(STORE)

    def connection(self, env, db):
        raise NotImplementedError

    def log_target(self, env):
        raise NotImplementedError

    def ship(self, env, component, project_dir):
        return None


@pytest.fixture
def project(invoke, workdir, monkeypatch):
    STORE.clear()
    BOUND.clear()
    assert invoke("init", "p", *LOCAL, "--json").exit_code == 0
    monkeypatch.chdir(workdir / "p")
    return workdir / "p"


@pytest.fixture
def deployed(invoke, project, monkeypatch):
    assert invoke("init", "--deploy", *DEPLOY, "--json").exit_code == 0
    # setitem on the registry is undone after the test; register_provider would leak the fake.
    monkeypatch.setitem(provider_module._REGISTRY, "aws", FakeCloud)
    return project


def test_local_value_from_stdin_lands_in_env(invoke, project):
    r = invoke("secret", "set", "HUBSPOT_API_KEY", "--json", input="s3cret\n")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {
        "name": "HUBSPOT_API_KEY",
        "env": "local",
        "copied": False,
        "ok": True,
    }
    assert dotenv_values(project / ".env", interpolate=False)["HUBSPOT_API_KEY"] == "s3cret"
    assert "s3cret" not in r.stdout


def test_set_strips_one_trailing_newline(invoke, project):
    assert invoke("secret", "set", "K", input="v\r\n").exit_code == 0
    assert dotenv_values(project / ".env", interpolate=False)["K"] == "v"
    assert invoke("secret", "set", "K2", input="a b\n").exit_code == 0
    assert dotenv_values(project / ".env", interpolate=False)["K2"] == "a b"


def test_set_refuses_empty_value(invoke, project):
    r = invoke("secret", "set", "K", "--json", input="\n")
    assert r.exit_code == 2 and "empty" in json.loads(r.stdout)["error"]
    assert not (project / ".env").exists() or "K" not in dotenv_values(project / ".env")


def test_value_as_argument_is_refused(invoke, project):
    r = invoke("secret", "set", "HUBSPOT_API_KEY", "abc123", "--json")
    assert r.exit_code == 2
    assert "standard input" in json.loads(r.stdout)["error"]
    assert "abc123" not in r.stdout + r.stderr
    assert not (project / ".env").exists()


def test_set_without_a_name_is_usage(invoke, project):
    r = invoke("secret", "set", "--json", input="v\n")
    assert r.exit_code == 2 and "name" in json.loads(r.stdout)["error"]


def test_cloud_value_goes_to_the_mocked_store(invoke, deployed):
    r = invoke("secret", "set", "HUBSPOT_API_KEY", "--env", "prod", "--json", input="cloudy\n")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert STORE == {"HUBSPOT_API_KEY": "cloudy"}
    assert set(BOUND) == {"prod"}
    assert not (deployed / ".env").exists() or "HUBSPOT_API_KEY" not in dotenv_values(
        deployed / ".env"
    )


def test_from_local_copies_without_printing(invoke, deployed):
    assert invoke("secret", "set", "HUBSPOT_API_KEY", input="loc4l\n").exit_code == 0
    r = invoke("secret", "set", "HUBSPOT_API_KEY", "--env", "prod", "--from-local", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert STORE["HUBSPOT_API_KEY"] == "loc4l"
    assert "loc4l" not in r.stdout and "loc4l" not in r.stderr
    assert json.loads(r.stdout)["copied"] is True
    record = read_record(deployed, "secrets-prod")
    assert record["env"] == "prod" and record["copied"] == ["HUBSPOT_API_KEY"]
    assert "loc4l" not in json.dumps(record)


def test_a_cloud_write_is_recorded_by_name_as_stored(invoke, deployed):
    r = invoke("secret", "set", "DEPLOY_SNOWFLAKE_USER", "--env", "prod", input="VECTEN_DEPLOY\n")
    assert r.exit_code == 0, r.stdout + r.stderr
    record = read_record(deployed, "secrets-prod")
    record.pop("recorded_at")
    assert record == {"env": "prod", "copied": [], "stored": ["DEPLOY_SNOWFLAKE_USER"]}
    assert "VECTEN_DEPLOY" not in json.dumps(record)


def test_stored_and_copied_names_accumulate_side_by_side(invoke, deployed):
    assert invoke("secret", "set", "HUBSPOT_API_KEY", input="loc4l\n").exit_code == 0
    assert invoke("secret", "set", "B_KEY", "--env", "prod", input="b\n").exit_code == 0
    assert (
        invoke("secret", "set", "HUBSPOT_API_KEY", "--env", "prod", "--from-local").exit_code == 0
    )
    assert invoke("secret", "set", "A_KEY", "--env", "prod", input="a\n").exit_code == 0
    record = read_record(deployed, "secrets-prod")
    record.pop("recorded_at")
    assert record == {"env": "prod", "copied": ["HUBSPOT_API_KEY"], "stored": ["A_KEY", "B_KEY"]}


def test_a_local_write_records_nothing(invoke, deployed):
    assert invoke("secret", "set", "K", input="v\n").exit_code == 0
    assert read_record(deployed, "secrets-prod") is None


def test_from_local_without_a_local_value_is_prerequisite(invoke, deployed):
    r = invoke("secret", "set", "NOPE", "--env", "prod", "--from-local", "--json")
    assert r.exit_code == 3 and "vecten secret set NOPE" in json.loads(r.stdout)["error"]


def test_from_local_without_env_is_usage(invoke, project):
    r = invoke("secret", "set", "K", "--from-local", "--json")
    assert r.exit_code == 2


def test_env_without_deploy_layer_is_refused(invoke, project):
    r = invoke("secret", "set", "K", "--env", "prod", "--json", input="v\n")
    assert r.exit_code == 4 and "init --deploy" in json.loads(r.stdout)["error"]
    # Empty stdin would be a USAGE refusal; REFUSED shows the cloud is checked before the read.
    r = invoke("secret", "set", "K", "--env", "prod", "--json")
    assert r.exit_code == 4 and "init --deploy" in json.loads(r.stdout)["error"]


@pytest.mark.parametrize("env", ["../x", "a/b", "Prod", "../../../x", "local"])
def test_bad_env_name_is_usage(invoke, deployed, workdir, env):
    assert invoke("secret", "set", "K", input="loc4l\n").exit_code == 0
    # The traversal names reach above the project, so the snapshot covers the parent too.
    before = set(workdir.parent.rglob("*"))
    r = invoke("secret", "set", "K", "--env", env, "--from-local", "--json")
    assert r.exit_code == 2, r.stdout + r.stderr
    assert "environment name" in json.loads(r.stdout)["error"]
    listed = invoke("secret", "list", "--env", env, "--json")
    assert listed.exit_code == 2
    assert STORE == {}
    assert set(workdir.parent.rglob("*")) == before


def test_env_name_with_digits_and_dash_is_accepted(invoke, deployed):
    r = invoke("secret", "set", "K", "--env", "prod-2", "--json", input="v\n")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert STORE == {"K": "v"}


class LeakyCloud(FakeCloud):
    def put_secret(self, name, value):
        raise RuntimeError(f"value={value}")

    def list_secrets(self):
        raise RuntimeError("value=s3cret")


def test_provider_exception_text_never_reaches_output(invoke, deployed, monkeypatch):
    monkeypatch.setitem(provider_module._REGISTRY, "aws", LeakyCloud)
    r = invoke("secret", "set", "K", "--env", "prod", "--json", input="s3cret\n")
    assert r.exit_code == 5
    assert json.loads(r.stdout)["error"] == "the aws secrets store failed: RuntimeError"
    assert "s3cret" not in r.stdout + r.stderr
    human = invoke("secret", "set", "K", "--env", "prod", input="s3cret\n")
    assert human.exit_code == 5 and "s3cret" not in human.stdout + human.stderr
    listed = invoke("secret", "list", "--env", "prod")
    assert listed.exit_code == 5 and "s3cret" not in listed.stdout + listed.stderr


def test_prompt_path_hides_input(invoke, project, monkeypatch):
    from types import SimpleNamespace

    from vecten.cli.commands import secret as secret_module

    calls = []

    def fake_prompt(text, **kwargs):
        calls.append(kwargs)
        return "typed"

    monkeypatch.setattr(
        secret_module, "sys", SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True))
    )
    monkeypatch.setattr(secret_module.typer, "prompt", fake_prompt)
    r = invoke("secret", "set", "K", "--json")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert calls == [{"hide_input": True}]
    assert dotenv_values(project / ".env", interpolate=False)["K"] == "typed"
    assert "typed" not in r.stdout


@pytest.mark.parametrize(
    "name", ["AIRFLOW_DB_PASSWORD", "AIRFLOW_ADMIN_USER", "AIRFLOW_FERNET_KEY"]
)
def test_set_refuses_stack_managed_names(invoke, project, name):
    r = invoke("secret", "set", name, "--json", input="override\n")
    assert r.exit_code == 4, r.stdout + r.stderr
    error = json.loads(r.stdout)["error"]
    assert "vecten up" in error and ".env" in error
    assert "override" not in r.stdout + r.stderr
    assert not (project / ".env").exists() or name not in dotenv_values(project / ".env")


def test_stack_managed_names_go_to_a_cloud_env(invoke, deployed):
    r = invoke("secret", "set", "AIRFLOW_DB_PASSWORD", "--env", "prod", "--json", input="db\n")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert STORE == {"AIRFLOW_DB_PASSWORD": "db"}


def test_secret_set_refuses_the_admin_password_for_an_environment(invoke, deployed):
    r = invoke(
        "secret", "set", "WAREHOUSE_ADMIN_PASSWORD", "--env", "prod", "--json", input="adm1n\n"
    )
    assert r.exit_code == 4, r.stdout + r.stderr
    assert "never" in json.loads(r.stdout)["error"]
    assert STORE == {}
    r = invoke("secret", "set", "WAREHOUSE_ADMIN_PASSWORD", "--json", input="adm1n\n")
    assert r.exit_code == 0, r.stdout + r.stderr


def test_list_shows_names_only(invoke, project):
    invoke("secret", "set", "B_KEY", input="2\n")
    invoke("secret", "set", "A_KEY", input="1\n")
    r = invoke("secret", "list", "--json")
    assert r.exit_code == 0
    assert json.loads(r.stdout) == {"env": "local", "names": ["A_KEY", "B_KEY"], "ok": True}
    human = invoke("secret", "list")
    assert human.stdout.splitlines() == ["A_KEY", "B_KEY"]
    assert "1" not in human.stdout and "2" not in human.stdout


def test_list_cloud_names(invoke, deployed):
    STORE["Z"] = "z"
    r = invoke("secret", "list", "--env", "staging", "--json")
    assert json.loads(r.stdout)["names"] == ["Z"]
    assert set(BOUND) == {"staging"}


def test_bad_secret_name_is_usage(invoke, project):
    r = invoke("secret", "set", "bad-name", "--json", input="v\n")
    assert r.exit_code == 2


def test_bad_secret_name_is_refused_before_the_cloud(invoke, deployed):
    r = invoke("secret", "set", "bad-name", "--env", "prod", "--json", input="v\n")
    assert r.exit_code == 2
    assert STORE == {}


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


def test_list_exits_three_on_an_unreadable_env(invoke, project, monkeypatch):
    assert invoke("secret", "set", "A", "--json", input="1\n").exit_code == 0
    deny_env(monkeypatch)
    result = invoke("secret", "list", "--json")
    assert result.exit_code == 3, result.output
    assert ".env" in result.output
