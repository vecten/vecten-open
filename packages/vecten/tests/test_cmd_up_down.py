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
        if args[:2] == ["config", "--services"]:
            names = []
            for path in files:
                if not path.is_file():
                    continue
                for name, svc in (
                    (yaml.safe_load(path.read_text()) or {}).get("services", {}).items()
                ):
                    if not svc.get("profiles") or set(svc["profiles"]) & set(profiles):
                        names.append(name)
            return subprocess.CompletedProcess(args, 0, "\n".join(dict.fromkeys(names)), "")
        if args[:1] == ["ps"] and "--format" in args:
            rows = [{"Service": n, "State": "running", "Health": "starting"} for n in args[3:]]
            return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, rows)), "")
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


def test_up_refuses_an_unknown_setting_before_anything_starts(invoke, workdir, fake, monkeypatch):
    from vecten_shared.output import ExitCode

    from .settings_fixtures import with_settings

    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    with_settings(workdir / "p", {"dialy_cap": 500})
    r = invoke("up", "--json")
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert "settings.acme.dialy_cap" in r.stdout and "local" in r.stdout
    assert fake.calls == []


def test_up_refuses_a_binding_the_record_does_not_declare(invoke, workdir, fake, monkeypatch):
    from vecten_shared.output import ExitCode

    from .services_fixtures import with_services

    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    with_services(workdir / "p", {"portal": {}})
    r = invoke("up", "--json")
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert "services.acme.portal" in r.stdout
    assert fake.calls == []


def _in_repo_bot(root, monkeypatch, command):
    from vecten import extensions
    from vecten_shared.services import parse_service

    from .services_fixtures import BOT, with_services

    with_services(
        root,
        {"bot": {}},
        declared={"bot": {**BOT, "routes": {}}},
        source_path="extensions/acme",
        metadata_sha256="0" * 64,
    )
    seen = []

    def load(config, candidates=None, only=None):
        seen.append(only)
        value = {**BOT, "routes": {}, "command": command}
        return {"acme": {"bot": parse_service("acme", "bot", value)}}

    monkeypatch.setattr(extensions, "load_services", load)
    monkeypatch.setattr(extensions, "installed_from", lambda name, path: True)
    return seen


def test_up_refuses_an_in_repo_service_changed_since_the_lock(invoke, workdir, fake, monkeypatch):
    from vecten_shared.output import ExitCode

    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    seen = _in_repo_bot(workdir / "p", monkeypatch, ["python", "-m", "acme.bot2"])
    r = invoke("up", "--json")
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert "service bot differs from extensions.lock.json" in r.stdout
    assert "vecten lock" in r.stdout
    assert seen == [{"acme"}]
    assert fake.calls == []


def test_up_refuses_an_in_repo_app_changed_since_the_lock(invoke, workdir, fake, monkeypatch):
    from vecten import extensions
    from vecten_shared.apps import parse_app
    from vecten_shared.output import ExitCode

    from .services_fixtures import with_services

    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    app = {
        "title": "Overview",
        "description": "",
        "kind": "container",
        "runtime": "streamlit",
        "entry": "acme/app.py",
        "datasets": ["orders"],
        "groups": ["default"],
    }
    with_services(
        workdir / "p",
        {},
        declared={},
        source_path="extensions/acme",
        metadata_sha256="0" * 64,
        apps={"overview": parse_app("acme", "overview", app).app},
    )
    changed = {**app, "datasets": ["orders", "customers"]}
    monkeypatch.setattr(extensions, "load_services", lambda config, candidates=None, only=None: {})
    monkeypatch.setattr(
        extensions,
        "load_apps",
        lambda config, candidates=None, only=None: {
            "acme": {"overview": parse_app("acme", "overview", changed)}
        },
    )
    monkeypatch.setattr(extensions, "installed_from", lambda name, path: True)
    r = invoke("up", "--json")
    assert r.exit_code == ExitCode.CONFIG, r.stdout
    assert "app overview differs from extensions.lock.json" in r.stdout
    assert fake.calls == []


def test_up_reads_only_in_repo_declarations_and_accepts_an_unchanged_one(
    invoke, workdir, fake, monkeypatch
):
    from .services_fixtures import BOT

    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    seen = _in_repo_bot(workdir / "p", monkeypatch, BOT["command"])
    r = invoke("up", "--json")
    assert "differs from extensions.lock.json" not in r.stdout, r.stdout
    assert seen == [{"acme"}]


def _bound_bot(root, monkeypatch, **binding):
    from .services_fixtures import BOT, with_services

    declared = {**BOT, "routes": {"webhook": BOT["routes"]["webhook"]}, "memory_mb": 384}
    worker = {"command": ["run"], "health": {"command": ["true"]}, "cpus": 0.25}
    bindings = {
        "bot": {"hostname": "bot", "routes": {"webhook": {"access": "public"}}, **binding},
        "worker": {},
    }
    with_services(root, bindings, declared={"bot": declared, "worker": worker})
    monkeypatch.setattr(up_mod, "write_project_layer", lambda *a, **k: None)


def test_up_starts_services_after_the_stack_and_never_waits_for_them(
    invoke, workdir, fake, monkeypatch
):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    _bound_bot(workdir / "p", monkeypatch)
    r = invoke("up", "--json")
    assert r.exit_code == 0, r.stdout
    assert ["config", "--services"] in fake.calls
    stack_up, services_up, ps = [c for c in fake.calls if c[:1] in (["up"], ["ps"])]
    assert stack_up[:6] == ["up", "-d", "--build", "--wait", "--wait-timeout", "240"]
    assert "nginx" in stack_up[6:] and "airflow-api-server" in stack_up[6:]
    assert not [name for name in stack_up if name.startswith("svc-")]
    assert services_up == ["up", "-d", "--no-build", "svc-acme-bot", "svc-acme-worker"]
    assert ps == ["ps", "--format", "json", "svc-acme-bot", "svc-acme-worker"]
    doc = json.loads(r.stdout)
    assert doc["services"] == {"acme.bot": "starting", "acme.worker": "starting"}
    assert doc["limits"] == {"memory_mb": 640, "cpus": 0.75}
    text = invoke("up").stdout
    assert "service acme.bot: starting" in text
    assert "services reserve 640 MB and 0.75 CPUs" in text


def test_up_without_services_starts_the_stack_as_one_call(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    r = invoke("up", "--json")
    assert r.exit_code == 0, r.stdout
    assert [c for c in fake.calls if c[:1] == ["up"]] == [
        ["up", "-d", "--build", "--wait", "--wait-timeout", "240"]
    ]
    doc = json.loads(r.stdout)
    assert doc["services"] == {} and doc["limits"] == {"memory_mb": 0, "cpus": 0}


def test_a_service_kept_off_the_laptop_is_not_started(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    _bound_bot(workdir / "p", monkeypatch, local=False)
    r = invoke("up", "--json")
    assert r.exit_code == 0, r.stdout
    assert ["up", "-d", "--no-build", "svc-acme-worker"] in fake.calls
    assert json.loads(r.stdout)["services"] == {"acme.worker": "starting"}


def test_a_service_compose_cannot_start_is_reported_and_the_stack_stays_up(
    invoke, workdir, fake, monkeypatch
):
    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    _bound_bot(workdir / "p", monkeypatch)
    real = fake.compose

    def compose(project_root, files, profiles, args):
        if args[:3] == ["up", "-d", "--no-build"]:
            fake.calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "no such image")
        return real(project_root, files, profiles, args)

    monkeypatch.setattr(fake, "compose", compose)
    r = invoke("up", "--json")
    assert r.exit_code == 0, r.stdout
    services = json.loads(r.stdout)["services"]
    assert services["acme.bot"].startswith("not started: no such image")


def test_services_an_override_file_adds_start_with_the_stack(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    root = workdir / "p"
    monkeypatch.chdir(root)
    _bound_bot(root, monkeypatch)
    override = root / "orchestration" / "docker-compose.override.yml"
    override.parent.mkdir(exist_ok=True)
    override.write_text(yaml.safe_dump({"services": {"mailhog": {"image": "mailhog"}}}))
    assert invoke("up", "--json").exit_code == 0
    stack_up = next(c for c in fake.calls if c[:1] == ["up"])
    assert "mailhog" in stack_up


def test_up_skips_the_check_when_this_checkout_is_not_installed(invoke, workdir, fake, monkeypatch):
    from vecten import extensions

    invoke("init", "p", *LOCAL, "--json")
    monkeypatch.chdir(workdir / "p")
    seen = _in_repo_bot(workdir / "p", monkeypatch, ["python", "-m", "acme.bot2"])
    asked = []

    def installed_from(name, path):
        asked.append((name, path))
        return False

    monkeypatch.setattr(extensions, "installed_from", installed_from)
    r = invoke("up", "--json")
    assert "differs from extensions.lock.json" not in r.stdout, r.stdout
    assert asked == [("acme", workdir / "p" / "extensions" / "acme")]
    assert seen == []


def _laptop_conf(root):
    return (root / ".vecten" / "stack" / "nginx" / "conf.d" / "default.conf").read_text()


def test_up_serves_a_routed_service_at_its_localhost_name_with_its_own_certificate(
    invoke, workdir, fake, monkeypatch
):
    invoke("init", "p", *LOCAL, "--json")
    root = workdir / "p"
    monkeypatch.chdir(root)
    _bound_bot(root, monkeypatch)
    r = invoke("up", "--json")
    assert r.exit_code == 0, r.stdout
    conf = _laptop_conf(root)
    assert "server_name bot.localhost;" in conf
    assert "ssl_certificate /etc/nginx/certs/bot.localhost.crt;" in conf
    assert "set $upstream http://svc-acme-bot:8000;" in conf
    assert (root / ".vecten" / "stack" / "certs" / "bot.localhost.crt").is_file()
    doc = json.loads(r.stdout)
    assert doc["routes"] == {"acme.bot.webhook": "https://bot.localhost:8443/api/messages"}
    assert "route acme.bot.webhook: https://bot.localhost:8443/api/messages" in invoke("up").stdout


def test_up_ignores_a_routes_allow_list(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    root = workdir / "p"
    monkeypatch.chdir(root)
    _bound_bot(root, monkeypatch, routes={"webhook": {"access": "public", "allow": ["10.0.0.0/8"]}})
    assert invoke("up", "--json").exit_code == 0
    conf = _laptop_conf(root)
    assert "server_name bot.localhost;" in conf
    assert "allow 10.0.0.0/8;" not in conf


def test_up_does_not_serve_a_service_kept_off_the_laptop(invoke, workdir, fake, monkeypatch):
    invoke("init", "p", *LOCAL, "--json")
    root = workdir / "p"
    monkeypatch.chdir(root)
    _bound_bot(root, monkeypatch, local=False)
    r = invoke("up", "--json")
    assert r.exit_code == 0, r.stdout
    assert "bot.localhost" not in _laptop_conf(root)
    assert json.loads(r.stdout)["routes"] == {}


def test_up_without_services_writes_the_nginx_of_before(invoke, workdir, fake, monkeypatch):
    from vecten.stack.compose import local_hostnames
    from vecten.stack.nginx import generate_nginx_conf

    invoke("init", "p", *LOCAL, "--json")
    root = workdir / "p"
    monkeypatch.chdir(root)
    assert invoke("up", "--json").exit_code == 0
    expected = generate_nginx_conf(["platform", "mcp"], local_hostnames(), "local")
    assert _laptop_conf(root) == expected
