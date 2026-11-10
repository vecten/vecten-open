import json
import socket
import stat
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml
from dotenv import dotenv_values

from vecten.stack import docker as docker_mod
from vecten.stack import up as up_mod
from vecten_shared.config import load_config, save_config, stack_project_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import write_record

LOCAL = ("--name", "acme", "--users", "8", "--source", "hubspot")


class FakeDocker:
    def __init__(self, available=True, running=True, fail_up=False, images=True):
        self._available, self._running, self._fail_up = available, running, fail_up
        self._images = images
        self.calls: list[list[str]] = []
        self.profiles: list[list[str]] = []
        self.labelled = ["vecten-acme-x_pgdata"]
        self.label_queries: list[str] = []
        self.removed: list[list[str]] = []

    def available(self):
        return self._available

    def image_exists(self, tag):
        return self._images

    def compose(self, project_root, files, profiles, args):
        self.calls.append(args)
        self.profiles.append(list(profiles))
        if args[:1] == ["up"] and self._fail_up:
            return subprocess.CompletedProcess(args, 1, "", "service nginx failed")
        if args[:1] == ["ps"]:
            return subprocess.CompletedProcess(args, 0, "abc123\n" if self._running else "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def volumes_by_label(self, project):
        self.label_queries.append(project)
        return list(self.labelled)

    def remove_volumes(self, names):
        self.removed.append(names)
        return subprocess.CompletedProcess(["volume", "rm", *names], 0, "", "")


@pytest.fixture
def fake(monkeypatch):
    f = FakeDocker()
    monkeypatch.setattr(up_mod, "get_runner", lambda: f)
    monkeypatch.setattr(up_mod, "wait_for_front_doors", lambda urls, timeout: None)
    return f


def test_up_generates_stack_and_prints_urls(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("up", "--json")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert doc["urls"] == {
        "airflow": "https://airflow.localhost:8443",
        "mcp": "https://mcp.localhost:8443",
    }
    assert doc["profiles"] == ["platform", "mcp", "local"]
    assert doc["airflow_api"] == "http://127.0.0.1:8482"
    assert doc["airflow_user"] == "admin"
    record = json.loads((workdir / "p" / ".vecten" / "records" / "up.json").read_text())
    assert record["airflow_api"] == "http://127.0.0.1:8482"
    assert record["airflow_user"] == "admin"
    password = dotenv_values(workdir / "p" / ".env")["AIRFLOW_ADMIN_PASSWORD"]
    assert password not in r.stdout and password not in json.dumps(record)
    human = invoke("up")
    assert "airflow_api: http://127.0.0.1:8482" in human.stdout
    assert "airflow_user: admin" in human.stdout
    assert "AIRFLOW_ADMIN_PASSWORD" in human.stdout and password not in human.stdout
    stack = workdir / "p" / ".vecten" / "stack"
    for rel in (
        "docker-compose.yml",
        "nginx/conf.d/default.conf",
        "certs/airflow.localhost.crt",
        "build/airflow.Dockerfile",
        "build/mcp.Dockerfile",
        "build/requirements.txt",
        "airflow_passwords.json",
        "pg-init/vecten/warehouse_setup.sql",
        "pg-init/10-vecten.sh",
    ):
        assert (stack / rel).is_file(), rel
    assert fake.calls[0][:3] == ["up", "-d", "--build"]
    assert fake.profiles[0] == ["platform", "mcp", "local"]
    assert (workdir / "p" / ".vecten" / "records" / "up.json").is_file()
    assert "AIRFLOW_BASIC_AUTH_PASSWORD=" not in (workdir / "p" / ".env").read_text()
    assert not (stack / "nginx" / "htpasswd").exists()


def test_up_mcp_only(invoke, workdir, fake, monkeypatch):
    invoke(
        "init",
        "p",
        "--name",
        "acme",
        "--users",
        "3",
        "--has-warehouse",
        "yes",
        "--warehouse-url",
        "postgres://h/dw",
        "--json",
    )
    monkeypatch.chdir(workdir / "p")
    doc = json.loads(invoke("up", "--json").stdout)
    assert doc["urls"] == {"mcp": "https://mcp.localhost:8443"} and doc["profiles"] == [
        "mcp",
        "local",
    ]
    assert fake.profiles[0] == ["mcp", "local"]
    assert "airflow_api" not in doc and "airflow_user" not in doc


def test_up_docker_daemon_down(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    monkeypatch.setattr(up_mod, "get_runner", lambda: FakeDocker(available=False))
    r = invoke("up", "--json")
    assert r.exit_code == 3 and "Docker" in json.loads(r.stdout)["error"]


def test_up_compose_failure_is_tool_failed(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    monkeypatch.setattr(up_mod, "get_runner", lambda: FakeDocker(fail_up=True))
    r = invoke("up", "--json")
    assert r.exit_code == 5 and "nginx" in json.loads(r.stdout)["error"]


def test_down_stops_running_stack(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    invoke("up", "--json")
    r = invoke("down", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["stopped"] is True
    assert fake.calls[-1][:1] == ["down"]


def test_down_without_stack_dir(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("down", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["stopped"] is False


def test_down_when_nothing_running(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    f = FakeDocker(running=False)
    monkeypatch.setattr(up_mod, "get_runner", lambda: f)
    monkeypatch.setattr(up_mod, "wait_for_front_doors", lambda urls, timeout: None)
    invoke("up", "--json")
    r = invoke("down", "--json")
    assert r.exit_code == 0 and "nothing was running" in json.loads(r.stdout)["message"]


def test_up_outside_project_refused(invoke):
    assert invoke("up", "--json").exit_code == 4


def test_up_writes_the_airflow_passwords_file_owner_only(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    assert invoke("up", "--json").exit_code == 0
    passwords = workdir / "p" / ".vecten" / "stack" / "airflow_passwords.json"
    assert stat.S_IMODE(passwords.stat().st_mode) == 0o600


def test_down_keeps_volumes_by_default(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    invoke("up", "--json")
    doc = json.loads(invoke("down", "--json").stdout)
    assert doc["volumes_removed"] is False and fake.calls[-1] == ["down"]


def test_down_volumes_removes_them(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    invoke("up", "--json")
    r = invoke("down", "--volumes", "--json")
    doc = json.loads(r.stdout)
    assert r.exit_code == 0 and doc["stopped"] is True and doc["volumes_removed"] is True
    assert fake.calls[-1] == ["down", "-v"]


def test_down_volumes_after_a_stop_still_removes_them(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    f = FakeDocker(running=False)
    monkeypatch.setattr(up_mod, "get_runner", lambda: f)
    monkeypatch.setattr(up_mod, "wait_for_front_doors", lambda urls, timeout: None)
    invoke("up", "--json")
    doc = json.loads(invoke("down", "--volumes", "--json").stdout)
    assert doc["stopped"] is False and doc["volumes_removed"] is True
    assert f.calls[-1] == ["down", "-v"]


def test_up_bounds_compose_wait_by_the_budget(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    assert invoke("up", "--json").exit_code == 0
    up_args = fake.calls[0]
    assert up_args[up_args.index("--wait-timeout") + 1] == "240"


def test_down_volumes_with_docker_unreachable_is_prerequisite(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    monkeypatch.setattr(up_mod, "get_runner", lambda: FakeDocker(available=False))
    r = invoke("down", "--volumes", "--json")
    assert r.exit_code == 3 and "Docker" in json.loads(r.stdout)["error"]


def test_down_volumes_without_stack_dir_removes_by_label(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    doc = json.loads(invoke("down", "--volumes", "--json").stdout)
    assert doc["stopped"] is False and doc["volumes_removed"] is True
    assert fake.label_queries == [stack_project_name(workdir / "p", "acme")]
    assert fake.removed == [["vecten-acme-x_pgdata"]]


def test_down_volumes_with_no_labelled_volumes_removes_nothing(invoke, workdir, fake, monkeypatch):
    fake.labelled = []
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("down", "--volumes", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["volumes_removed"] is True
    assert all(names == [] for names in fake.removed)


def test_remove_volumes_with_no_names_runs_no_docker(monkeypatch):
    def refuse(cmd, **kwargs):
        raise AssertionError(f"unexpected docker call {cmd}")

    monkeypatch.setattr(docker_mod.subprocess, "run", refuse)
    assert docker_mod.SubprocessDocker().remove_volumes([]).returncode == 0


def test_docker_info_timeout_means_unavailable(monkeypatch):
    seen = {}

    def hang(cmd, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(docker_mod.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(docker_mod.subprocess, "run", hang)
    assert docker_mod.SubprocessDocker().available() is False
    assert seen["timeout"] == 15


def test_front_door_wait_times_out_naming_the_component(monkeypatch):
    # Bound but never listening: connections are refused, and no other process can take the port.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        monkeypatch.setattr(up_mod, "LOCAL_HTTPS_PORT", port)
        with pytest.raises(Failure) as err:
            up_mod.wait_for_front_doors({"mcp": f"https://mcp.localhost:{port}"}, timeout=1)
    assert err.value.code == ExitCode.TOOL_FAILED and "mcp" in str(err.value)


def test_airflow_is_up_once_its_health_answers_through_the_front_door(monkeypatch):
    seen = []

    def get(self, url, **kwargs):
        seen.append(url)
        # Airflow's own login guards every other path, which answers 200 with its sign-in page.
        status = 200 if url.endswith("/api/v2/monitor/health") else 401
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", get)
    up_mod.wait_for_front_doors({"airflow": "https://airflow.localhost:8443"}, timeout=1)
    assert seen == [f"https://127.0.0.1:{up_mod.LOCAL_HTTPS_PORT}/api/v2/monitor/health"]


def test_down_uses_the_profiles_up_started_and_records_the_stop(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    assert invoke("up", "--json").exit_code == 0
    config = load_config(proj)
    config.components = ["mcp"]
    save_config(proj, config)
    r = invoke("down", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["stopped"] is True
    assert fake.calls[-1] == ["down"]
    assert fake.profiles[-2:] == [["local", "mcp", "platform"]] * 2
    records = proj / ".vecten" / "records"
    assert not (records / "up.json").exists()
    assert json.loads((records / "down.json").read_text())["profiles"] == [
        "local",
        "mcp",
        "platform",
    ]


def test_down_stops_the_union_of_recorded_and_configured_profiles(
    invoke, workdir, fake, monkeypatch
):
    invoke("init", "p", *LOCAL, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    assert invoke("up", "--json").exit_code == 0
    # A failed `up` after adding a component leaves the record naming the earlier profiles.
    write_record(proj, "up", {"profiles": ["mcp", "local"]})
    assert load_config(proj).components == ["platform", "mcp"]
    r = invoke("down", "--json")
    assert r.exit_code == 0, r.stdout
    assert fake.calls[-1] == ["down"]
    assert fake.profiles[-1] == ["local", "mcp", "platform"]


def test_down_without_an_up_record_uses_the_config(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    invoke("up", "--json")
    (proj / ".vecten" / "records" / "up.json").unlink()
    assert invoke("down", "--json").exit_code == 0
    assert fake.profiles[-1] == ["local", "mcp", "platform"]


def test_build_context_holds_only_the_project_layer(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    proj = workdir / "p"
    monkeypatch.chdir(proj)
    assert invoke("up", "--json").exit_code == 0
    stack = proj / ".vecten" / "stack"
    doc = yaml.safe_load((stack / "docker-compose.yml").read_text())
    contexts = {Path(svc["build"]["context"]) for svc in doc["services"].values() if "build" in svc}
    assert contexts == {stack / "build"}
    assert sorted(p.name for p in (stack / "build").iterdir()) == [
        "airflow.Dockerfile",
        "mcp.Dockerfile",
        "requirements.txt",
    ]
    for secret in ("airflow_passwords.json", "certs"):
        assert (stack / secret).exists() and stack / "build" not in (stack / secret).parents


def test_up_failure_without_local_base_images_says_how_to_build_them(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    monkeypatch.delenv("VECTEN_IMAGE_REGISTRY", raising=False)
    monkeypatch.setattr(up_mod, "get_runner", lambda: FakeDocker(fail_up=True, images=False))
    r = invoke("up", "--json")
    error = json.loads(r.stdout)["error"]
    assert r.exit_code == 5 and "nginx" in error
    assert "no local base images found" in error and "vecten dev build-images" in error


def test_up_failure_with_local_base_images_has_no_build_hint(invoke, workdir, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    monkeypatch.setattr(up_mod, "get_runner", lambda: FakeDocker(fail_up=True))
    r = invoke("up", "--json")
    assert r.exit_code == 5 and "build-images" not in json.loads(r.stdout)["error"]


def test_up_on_snowflake_leaves_the_mcp_out(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--warehouse", "snowflake", "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("up", "--json")
    assert r.exit_code == 0, r.stdout
    doc = json.loads(r.stdout)
    assert fake.profiles[0] == ["platform", "local"]
    assert doc["profiles"] == ["platform", "local"]
    assert doc["urls"] == {"airflow": "https://airflow.localhost:8443"}
    assert doc["left_out"] == ["mcp"]
    record = json.loads((workdir / "p" / ".vecten" / "records" / "up.json").read_text())
    assert record["profiles"] == ["platform", "local"]
    nginx = (workdir / "p" / ".vecten" / "stack" / "nginx" / "conf.d" / "default.conf").read_text()
    assert "mcp.localhost" not in nginx
    human = invoke("up")
    assert "the MCP runs only deployed" in human.stdout


def test_up_on_an_existing_snowflake_warehouse_has_nothing_to_start(
    invoke, workdir, fake, monkeypatch
):
    url = "snowflake://ab12345.eu-central-1/analytics"
    flags = ("--has-warehouse", "yes", "--warehouse-url", url)
    invoke("init", "p", "--name", "acme", "--users", "3", *flags, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("up", "--json")
    assert r.exit_code == 4, r.stdout
    assert "runs only deployed" in json.loads(r.stdout)["error"]
    assert fake.calls == []
