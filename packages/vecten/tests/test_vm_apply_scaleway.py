"""The VM agent on Scaleway: the stdlib backend, the HTTP seam and the pull watcher."""

import base64
import errno
import fcntl
import hashlib
import http.client
import http.server
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import threading
import types
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from vecten_shared.provider import scope_for
from vecten_shared.provider.scaleway import scaleway_secret_path

from .test_vm_apply import APPLY, DOMAIN, FakeRun, compose_calls, load_agent, stage, with_certs

REGION = "fr-par"
PROJECT_ID = "0123abcd-1111-2222-3333-444455556666"
BUCKET = "vecten-acme-prod-artifacts-0123abcd"
PREFIX = "/vecten/acme/prod/"
ACCESS = "SCWACCESSKEY0000001"
SECRET = "11111111-2222-3333-4444-555555555555"
ROTATED_ACCESS = "SCWACCESSKEY0000002"
ROTATED_SECRET = "66666666-7777-8888-9999-000000000000"
SCW_API = f"https://api.scaleway.com/secret-manager/v1beta1/regions/{REGION}"
S3_ROOT = f"https://s3.{REGION}.scw.cloud/{BUCKET}/"
METADATA_KEY = "http://169.254.42.42/user_data/vecten-key"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


@pytest.fixture
def agent(tmp_path, monkeypatch):
    module = load_agent()
    monkeypatch.setattr(module, "LETSENCRYPT", tmp_path / "letsencrypt")
    monkeypatch.setattr(module, "RUN_DIR", tmp_path / "run" / "vecten")
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "var" / "lib" / "vecten")
    monkeypatch.setattr(module, "WATCH_CONFIG", tmp_path / "etc" / "vecten" / "watch.json")
    monkeypatch.setattr(module, "AGENT_COPY", tmp_path / "opt" / "agent" / "apply.py")
    return module


def scw_outputs(shape="standard"):
    if shape == "minimal":
        hosts = {"main": (["platform", "mcp"], [f"airflow.{DOMAIN}", f"mcp.{DOMAIN}"])}
        postgres = {"state": ["airflow", "mcp_state", "warehouse"]}
        setup = "main"
    else:
        hosts = {
            "platform": (["platform"], [f"airflow.{DOMAIN}"]),
            "mcp": (["mcp"], [f"mcp.{DOMAIN}"]),
        }
        postgres = {"state": ["airflow", "mcp_state"], "warehouse": ["warehouse"]}
        setup = "mcp"
    return {
        "environment": "prod",
        "region": REGION,
        "hosts": {
            name: {
                "instance_id": f"srv-{name}",
                "public_ip": "203.0.113.7",
                "components": components,
                "hostnames": names,
            }
            for name, (components, names) in hosts.items()
        },
        "setup_host": setup,
        "bucket": BUCKET,
        "secret_prefix": PREFIX,
        "postgres": {
            key: {
                "host": f"{key}.internal",
                "port": 5432,
                "databases": databases,
                "master_secret": f"master-{key}-id",
            }
            for key, databases in postgres.items()
        },
    }


def stage_scaleway(releases, out, name, commit="c1"):
    release = stage(releases, out, name, commit)
    path = release / ".vecten" / "ship" / "host.json"
    doc = json.loads(path.read_text())
    doc |= {
        "cloud": "scaleway",
        "project_id": PROJECT_ID,
        "plugin_dir": "/usr/local/lib/docker/cli-plugins",
    }
    path.write_text(json.dumps(doc))
    return release


class FakeScaleway:
    """Secret Manager, Object Storage and the metadata server behind the agent's HTTP seam."""

    def __init__(self, metadata_key=None):
        self.secrets = {}
        self.objects = {}
        self.metadata_key = metadata_key
        self.calls = []
        self.failures = []

    def add_secret(self, path, name, value=None, project_id=PROJECT_ID, secret_id=None):
        sid = secret_id or f"id-{len(self.secrets)}-{name}"
        versions = [] if value is None else [value]
        self.secrets[sid] = {
            "path": path,
            "name": name,
            "project_id": project_id,
            "versions": versions,
        }
        return sid

    def shells(self, out, names):
        for name in names:
            self.add_secret(f"{PREFIX}{scope_for(name)}".rstrip("/"), name)

    def value(self, name):
        for secret in self.secrets.values():
            if secret["name"] == name and secret["versions"]:
                return secret["versions"][-1]
        return None

    def fail(self, method, prefix, status, times=None):
        self.failures.append({"method": method, "prefix": prefix, "status": status, "left": times})

    def __call__(self, method, url, headers=None, body=None, privileged=False):
        headers = dict(headers or {})
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "body": body, "priv": privileged}
        )
        for failure in self.failures:
            if method == failure["method"] and url.startswith(failure["prefix"]):
                if failure["left"] is None or failure["left"] > 0:
                    if failure["left"] is not None:
                        failure["left"] -= 1
                    detail = b'{"type": "permissions_denied", "message": "secret-ish detail"}'
                    return failure["status"], detail
        if url == METADATA_KEY:
            assert privileged and method == "GET"
            if self.metadata_key is None:
                return 404, b""
            return 200, self.metadata_key.encode()
        if url.startswith(SCW_API):
            return self._secret_manager(method, url, headers, body)
        if url.startswith(S3_ROOT):
            return self._s3(method, url, headers, body)
        raise AssertionError(f"unexpected request {method} {url}")

    def _secret_manager(self, method, url, headers, body):
        assert headers["X-Auth-Token"] in (SECRET, ROTATED_SECRET)
        parts = urlsplit(url)
        path = parts.path[len(urlsplit(SCW_API).path) :]
        if method == "GET" and path == "/secrets":
            query = {k: v[0] for k, v in parse_qs(parts.query).items()}
            found = [
                {"id": sid, "path": s["path"], "name": s["name"]}
                for sid, s in self.secrets.items()
                if s["project_id"] == query["project_id"]
                and ("path" not in query or s["path"] == query["path"])
                and ("name" not in query or s["name"] == query["name"])
            ]
            return 200, json.dumps({"secrets": found, "total_count": len(found)}).encode()
        segments = path.strip("/").split("/")
        sid = unquote(segments[1])
        secret = self.secrets.get(sid)
        if secret is None:
            return 404, b'{"type": "not_found"}'
        if method == "GET" and segments[2:] == ["versions", "latest_enabled", "access"]:
            if not secret["versions"]:
                return 404, b'{"type": "not_found"}'
            data = base64.b64encode(secret["versions"][-1].encode()).decode()
            return 200, json.dumps({"secret_id": sid, "revision": 1, "data": data}).encode()
        if method == "POST" and segments[2:] == ["versions"]:
            assert headers["Content-Type"] == "application/json"
            doc = json.loads(body)
            secret["versions"].append(base64.b64decode(doc["data"]).decode())
            return 200, b'{"revision": 1}'
        raise AssertionError(f"unexpected Secret Manager call {method} {url}")

    def _s3(self, method, url, headers, body):
        auth = headers["Authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=")
        assert f"/{REGION}/s3/aws4_request" in auth
        payload = body or b""
        assert headers["x-amz-content-sha256"] == hashlib.sha256(payload).hexdigest()
        key = unquote(url[len(S3_ROOT) :])
        if method == "GET":
            if key in self.objects:
                return 200, self.objects[key]
            return 404, b"<Error><Code>NoSuchKey</Code></Error>"
        if method == "PUT":
            self.objects[key] = payload
            return 200, b""
        raise AssertionError(f"unexpected S3 call {method} {url}")

    def urls(self):
        return [c["url"] for c in self.calls]

    def s3_calls(self):
        return [c for c in self.calls if c["url"].startswith(S3_ROOT)]


def backend(agent, fake, key=(ACCESS, SECRET)):
    return agent.ScalewayBackend(key, REGION, PROJECT_ID, PREFIX, BUCKET, http=fake)


# ---------------------------------------------------------------- the secret path rule


@pytest.mark.parametrize(
    "name",
    [
        "AIRFLOW_DB_PASSWORD",
        "HUBSPOT_API_KEY",
        "MCP_STATE_DB_PASSWORD",
        "DEPLOY_SNOWFLAKE_USER",
        "ADMIN_ANNA_PASSWORD",
        "SETUP_STATE_ADMIN_PASSWORD",
        "SETUP_WAREHOUSE_ADMIN_PASSWORD",
    ],
)
@pytest.mark.parametrize(("project", "env"), [("acme", "prod"), ("big-co", "staging-2")])
def test_secret_location_matches_the_runtime_provider(name, project, env):
    agent = load_agent()
    store = agent.ScalewayBackend(
        (ACCESS, SECRET), REGION, PROJECT_ID, f"/vecten/{project}/{env}/", BUCKET
    )
    assert store.secret_location(name) == (
        scaleway_secret_path(project, env, scope_for(name)),
        name,
    )


# ---------------------------------------------------------------- Secret Manager


def test_read_secret_lists_by_path_and_name_then_reads_by_id(agent):
    fake = FakeScaleway()
    fake.add_secret("/vecten/acme/prod/platform", "LOADER_PASSWORD", "loader-pw", secret_id="sid-1")
    fake.add_secret("/vecten/other/prod/platform", "LOADER_PASSWORD", "other-pw")
    assert backend(agent, fake).read_secret("LOADER_PASSWORD") == "loader-pw"
    listing, access = fake.calls
    query = {k: v[0] for k, v in parse_qs(urlsplit(listing["url"]).query).items()}
    assert listing["method"] == "GET" and urlsplit(listing["url"]).path.endswith("/secrets")
    assert query["path"] == "/vecten/acme/prod/platform"
    assert query["name"] == "LOADER_PASSWORD" and query["project_id"] == PROJECT_ID
    assert access["url"] == f"{SCW_API}/secrets/sid-1/versions/latest_enabled/access"
    assert all(c["headers"]["X-Auth-Token"] == SECRET for c in fake.calls)


def test_a_missing_secret_or_a_shell_without_a_version_has_no_value(agent):
    fake = FakeScaleway()
    fake.add_secret(f"{PREFIX}platform", "AIRFLOW_DB_PASSWORD")
    store = backend(agent, fake)
    assert store.read_secret("AIRFLOW_DB_PASSWORD") is None
    assert store.secret_has_value("AIRFLOW_DB_PASSWORD") is False
    assert store.read_secret("LOADER_PASSWORD") is None
    assert store.secret_has_value("LOADER_PASSWORD") is False


def test_has_value_is_true_once_latest_enabled_answers(agent):
    fake = FakeScaleway()
    fake.add_secret(f"{PREFIX}mcp", "MCP_STATE_DB_PASSWORD", "x")
    assert backend(agent, fake).secret_has_value("MCP_STATE_DB_PASSWORD") is True


def test_add_secret_version_posts_base64_data_to_the_shell(agent):
    fake = FakeScaleway()
    sid = fake.add_secret(f"{PREFIX}platform", "AIRFLOW_JWT_SECRET")
    backend(agent, fake).add_secret_version("AIRFLOW_JWT_SECRET", "jwt-value/with+chars")
    assert fake.secrets[sid]["versions"] == ["jwt-value/with+chars"]
    post = fake.calls[-1]
    assert post["method"] == "POST" and post["url"] == f"{SCW_API}/secrets/{sid}/versions"
    assert json.loads(post["body"]) == {"data": base64.b64encode(b"jwt-value/with+chars").decode()}
    assert not any("jwt-value" in u for u in fake.urls())


def test_adding_a_version_without_the_terraform_shell_names_deploy(agent):
    with pytest.raises(agent.AgentError, match="does not exist.*vecten deploy"):
        backend(agent, FakeScaleway()).add_secret_version("AIRFLOW_JWT_SECRET", "v")


def test_a_secret_manager_error_names_the_status_and_type_never_the_message(agent):
    fake = FakeScaleway()
    fake.add_secret(f"{PREFIX}platform", "LOADER_PASSWORD", "pw")
    fake.fail("GET", f"{SCW_API}/secrets/", 403)
    with pytest.raises(agent.AgentError) as exc:
        backend(agent, fake).read_secret("LOADER_PASSWORD")
    message = str(exc.value)
    assert "HTTP 403 permissions_denied" in message and "LOADER_PASSWORD" in message
    assert "secret-ish" not in message


def test_read_reference_reads_a_master_secret_by_its_id(agent):
    fake = FakeScaleway()
    fake.add_secret(f"{PREFIX}setup", "SETUP_STATE_ADMIN_PASSWORD", '{"a": 1}', secret_id="m-1")
    assert backend(agent, fake).read_reference("m-1") == '{"a": 1}'
    (call,) = fake.calls
    assert call["url"] == f"{SCW_API}/secrets/m-1/versions/latest_enabled/access"


# ---------------------------------------------------------------- Object Storage


def test_get_object_reads_path_style_and_a_missing_key_is_none(agent):
    fake = FakeScaleway()
    fake.objects["bundles/platform/pointer.json"] = b"{}"
    store = backend(agent, fake)
    assert store.get_object("bundles/platform/pointer.json") == b"{}"
    assert store.get_object("bundles/platform/absent.json") is None
    assert fake.calls[0]["url"] == f"{S3_ROOT}bundles/platform/pointer.json"
    assert f"Credential={ACCESS}/" in fake.calls[0]["headers"]["Authorization"]
    assert SECRET not in json.dumps(fake.calls[0]["headers"])


def test_an_object_storage_error_other_than_404_names_its_status(agent):
    fake = FakeScaleway()
    fake.fail("GET", S3_ROOT, 403)
    with pytest.raises(agent.AgentError, match="HTTP 403") as exc:
        backend(agent, fake).get_object("bundles/platform/pointer.json")
    assert "secret-ish" not in str(exc.value)


def test_put_object_signs_the_body_hash(agent):
    fake = FakeScaleway()
    backend(agent, fake).put_object("platform/ship-status/x.json", b'{"ok": true}')
    assert fake.objects["platform/ship-status/x.json"] == b'{"ok": true}'


# ---------------------------------------------------------------- ship on Scaleway


def scw_store(fake, out):
    """Every secret a standard or minimal Scaleway environment holds once it has shipped."""
    for key in out["postgres"]:
        fake.add_secret(
            f"{PREFIX}setup",
            f"SETUP_{key.upper()}_ADMIN_PASSWORD",
            json.dumps({"username": "vecten_admin", "password": f"admin-{key}-pw"}),
            secret_id=f"master-{key}-id",
        )
    fake.shells(
        out,
        [
            "AIRFLOW_DB_PASSWORD",
            "LOADER_PASSWORD",
            "MCP_STATE_DB_PASSWORD",
            "MCP_PROVISIONER_PASSWORD",
            "MCP_IMPERSONATOR_PASSWORD",
            "AIRFLOW_FERNET_KEY",
            "AIRFLOW_API_SECRET_KEY",
            "AIRFLOW_JWT_SECRET",
            "AIRFLOW_ADMIN_PASSWORD",
            "AIRFLOW_BASIC_AUTH_PASSWORD",
        ],
    )


def write_key(agent, access=ACCESS, secret=SECRET):
    agent.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (agent.STATE_DIR / "scw-key").write_text(f"{access}:{secret}")


def ship_scaleway(agent, release, run, fake, monkeypatch):
    monkeypatch.setattr(agent, "http", fake)
    with_certs(agent, json.loads((release / ".vecten/ship/host.json").read_text())["hostnames"])
    agent.ship(
        release,
        run,
        releases=release.parent,
        sleep=lambda s: None,
        resolve=lambda h: ["203.0.113.7"],
    )


def test_minimal_ship_on_scaleway_generates_sets_up_and_starts(agent, tmp_path, monkeypatch):
    out = scw_outputs("minimal")
    release = stage_scaleway(tmp_path / "releases", out, "main")
    fake = FakeScaleway()
    scw_store(fake, out)
    write_key(agent)
    run = FakeRun()
    ship_scaleway(agent, release, run, fake, monkeypatch)
    assert not run.argv("aws")
    for name in ("AIRFLOW_DB_PASSWORD", "MCP_IMPERSONATOR_PASSWORD", "AIRFLOW_FERNET_KEY"):
        assert fake.value(name), name
    (psql,) = [c for c in run.calls if c["args"][:2] == ["docker", "run"] and "psql" in c["args"]]
    assert (
        psql["env"]["PGPASSWORD"] == "admin-state-pw"
        and psql["env"]["MASTER_ROLE"] == "vecten_admin"
    )
    (up,) = compose_calls(run)
    env = up["env"]
    assert env["VECTEN_PROVIDER"] == "scaleway"
    assert env["SCW_ACCESS_KEY"] == ACCESS and env["SCW_SECRET_KEY"] == SECRET
    assert env["SCW_DEFAULT_REGION"] == REGION and env["SCW_DEFAULT_PROJECT_ID"] == PROJECT_ID
    assert env["AWS_ACCESS_KEY_ID"] == ACCESS and env["AWS_SECRET_ACCESS_KEY"] == SECRET
    assert env["AWS_REGION"] == REGION
    assert env["AWS_ENDPOINT_URL_S3"] == "https://s3.fr-par.scw.cloud"
    assert env["VECTEN_ARTIFACT_URL"] == f"s3://{BUCKET}/platform/"
    assert env["AIRFLOW_REMOTE_BASE_LOG_FOLDER"] == f"s3://{BUCKET}/platform/logs"
    conn = json.loads(env["AIRFLOW_CONN_AWS_DEFAULT"])
    assert conn["conn_type"] == "aws"
    assert conn["login"] == ACCESS and conn["password"] == SECRET
    assert conn["extra"] == {"endpoint_url": "https://s3.fr-par.scw.cloud", "region_name": REGION}
    values = [v for s in fake.secrets.values() for v in s["versions"]] + [SECRET]
    for call in run.calls:
        for arg in call["args"]:
            assert not any(v in arg for v in values), call["args"]
    for url in fake.urls():
        assert not any(v in url for v in values), url


def test_a_scaleway_excerpt_masks_the_vms_key(agent, tmp_path, monkeypatch):
    out = scw_outputs("minimal")
    release = stage_scaleway(tmp_path / "releases", out, "main")
    fake = FakeScaleway()
    scw_store(fake, out)
    write_key(agent)
    rows = [("a1", "airflow-scheduler", "restarting", "Restarting (1)")]
    run = FakeRun(containers=rows, logs={"a1": (f"s3 refused secret {SECRET}\n", "")})

    def failing_up(args, env=None, input=None):
        if args[:2] == ["docker", "compose"] and "up" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        return run(args, env, input)

    with pytest.raises(agent.AgentError) as exc:
        ship_scaleway(agent, release, failing_up, fake, monkeypatch)
    assert exc.value.log == ("airflow-scheduler: s3 refused secret ***",)


def test_a_scaleway_plugin_lands_in_the_ubuntu_plugin_directory(agent, tmp_path):
    host = agent.load_host(stage_scaleway(tmp_path / "releases", scw_outputs(), "platform"))
    run = FakeRun(compose_version="2.20.0")
    url = (
        f"https://github.com/docker/compose/releases/download/v{host['compose_plugin_version']}/"
        "docker-compose-linux-x86_64"
    )
    run.downloads[url] = b"compose"
    run.downloads[url + ".sha256"] = (
        f"{hashlib.sha256(b'compose').hexdigest()} *docker-compose-linux-x86_64\n".encode()
    )
    agent.ensure_compose_plugin(host, run)
    (install,) = run.argv("install")
    assert install[-1] == "/usr/local/lib/docker/cli-plugins/docker-compose"


def test_scaleway_ship_without_the_key_file_names_it(agent, tmp_path, monkeypatch, capsys):
    release = stage_scaleway(tmp_path / "releases", scw_outputs(), "platform")
    monkeypatch.setattr(agent, "http", FakeScaleway())
    code = agent.main(["ship", "--release", str(release)], run=FakeRun())
    (line,) = capsys.readouterr().err.strip().splitlines()
    assert code == 1 and "generated secrets" in line and "scw-key" in line


def test_host_keys_carry_the_cloud_contract():
    agent = load_agent()
    for key in ("cloud", "region", "project_id", "bucket", "secret_prefix", "plugin_dir"):
        assert key in agent.HOST_KEYS
    assert agent.PLUGIN_DIRS == {
        "aws": "/usr/libexec/docker/cli-plugins",
        "scaleway": "/usr/local/lib/docker/cli-plugins",
    }


# ---------------------------------------------------------------- the watcher


def watch_config(agent, host="platform", components=("platform",)):
    agent.WATCH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    agent.WATCH_CONFIG.write_text(
        json.dumps(
            {
                "host": host,
                "components": list(components),
                "bucket": BUCKET,
                "region": REGION,
                "project_id": PROJECT_ID,
                "secret_prefix": PREFIX,
            }
        )
    )


def bundle_bytes(agent_text="# agent v2\n"):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in (
            (".vecten/ship/apply.py", agent_text.encode()),
            (".vecten/ship/host.json", b"{}"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def pointer(fake, commit, requested_at="2026-10-18T10:00:00Z", host="platform", force=None):
    doc = {"commit": commit, "requested_at": requested_at}
    if force is not None:
        doc["force"] = force
    fake.objects[f"bundles/{host}/pointer.json"] = json.dumps(doc).encode()


class ShipRun(FakeRun):
    """Extracts with the real tar and answers the release agent's ship with a fixed outcome; a
    ship that succeeds points `current` at its release, as the agent's swap does."""

    def __init__(self, ship_code=0, ship_stderr=""):
        super().__init__()
        self.ship_code = ship_code
        self.ship_stderr = ship_stderr

    def __call__(self, args, env=None, input=None):
        if args[0] == "tar":
            self.calls.append({"args": list(args), "env": dict(env or {}), "input": input})
            return subprocess.run(args, capture_output=True, text=True, check=False)
        if args[0] == sys.executable and "ship" in args:
            self.calls.append({"args": list(args), "env": dict(env or {}), "input": input})
            if self.ship_code == 0:
                release = args[args.index("--release") + 1]
                link = os.path.join(os.path.dirname(release), "current")
                if os.path.islink(link):
                    os.unlink(link)
                os.symlink(release, link)
            return subprocess.CompletedProcess(args, self.ship_code, "", self.ship_stderr)
        return super().__call__(args, env, input)

    def ships(self):
        return [c["args"] for c in self.calls if c["args"][0] == sys.executable]


def status(fake, commit, component="platform"):
    raw = fake.objects.get(f"{component}/ship-status/{commit}.json")
    return None if raw is None else json.loads(raw)


def ack_key(access, component="platform"):
    return f"{component}/key-ack/{hashlib.sha256(access.encode()).hexdigest()}.json"


def test_no_key_yet_makes_no_object_request_and_waits(agent, tmp_path):
    watch_config(agent)
    fake = FakeScaleway()
    line = agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    assert "waiting for key" in line
    assert fake.urls() == [METADATA_KEY]


def test_a_key_in_metadata_goes_to_a_root_only_file_and_is_acknowledged(agent, tmp_path):
    watch_config(agent)
    fake = FakeScaleway(metadata_key=f"{ACCESS}:{SECRET}")
    agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    key_file = agent.STATE_DIR / "scw-key"
    assert key_file.read_text() == f"{ACCESS}:{SECRET}"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(agent.STATE_DIR.stat().st_mode) == 0o700
    ack = fake.objects[ack_key(ACCESS)]
    assert SECRET not in ack.decode() and ACCESS not in ack.decode()
    assert json.loads(ack)["host"] == "platform"


def test_the_same_metadata_key_is_acknowledged_once(agent, tmp_path):
    watch_config(agent)
    fake = FakeScaleway(metadata_key=f"{ACCESS}:{SECRET}")
    for _ in range(2):
        agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    puts = [c for c in fake.s3_calls() if c["method"] == "PUT"]
    assert [unquote(c["url"][len(S3_ROOT) :]) for c in puts] == [ack_key(ACCESS)]


def test_once_metadata_drops_the_key_the_file_on_disk_serves(agent, tmp_path):
    watch_config(agent)
    fake = FakeScaleway(metadata_key=f"{ACCESS}:{SECRET}")
    agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    fake.metadata_key = None
    fake.calls.clear()
    line = agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    assert "no release requested" in line
    (get,) = fake.s3_calls()
    assert get["url"] == f"{S3_ROOT}bundles/platform/pointer.json"
    assert f"Credential={ACCESS}/" in get["headers"]["Authorization"]


def test_a_rotated_metadata_key_replaces_the_file_and_is_acknowledged(agent, tmp_path):
    watch_config(agent, host="main", components=("platform", "mcp"))
    write_key(agent)
    fake = FakeScaleway(metadata_key=f"{ROTATED_ACCESS}:{ROTATED_SECRET}")
    agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    assert (agent.STATE_DIR / "scw-key").read_text() == f"{ROTATED_ACCESS}:{ROTATED_SECRET}"
    assert ack_key(ROTATED_ACCESS) in fake.objects
    assert f"Credential={ROTATED_ACCESS}/" in fake.s3_calls()[-1]["headers"]["Authorization"]


def test_a_malformed_metadata_key_is_refused_without_quoting_it(agent, tmp_path):
    watch_config(agent)
    fake = FakeScaleway(metadata_key="not-a-key-pair")
    with pytest.raises(agent.AgentError) as exc:
        agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    assert "not-a-key-pair" not in str(exc.value) and "vecten-key" in str(exc.value)
    assert not (agent.STATE_DIR / "scw-key").exists()


def test_a_new_commit_is_downloaded_extracted_shipped_and_reported(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    pointer(fake, COMMIT_A)
    fake.objects[f"bundles/platform/{COMMIT_A}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=0)
    releases = tmp_path / "releases"
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    release = releases / COMMIT_A
    assert (release / ".vecten" / "ship" / "apply.py").read_text() == "# agent v2\n"
    (ship_args,) = run.ships()
    assert ship_args == [
        sys.executable,
        str(release / ".vecten" / "ship" / "apply.py"),
        "ship",
        "--release",
        str(release),
    ]
    (tar,) = run.argv("tar")
    assert "--no-same-owner" in tar
    assert status(fake, COMMIT_A) == {
        "ok": True,
        "line": f"shipped {COMMIT_A}",
        "requested_at": "2026-10-18T10:00:00Z",
        "running": COMMIT_A,
        "log": [],
    }
    # The watcher of later ships is the newest release's agent.
    assert agent.AGENT_COPY.read_text() == "# agent v2\n"
    assert not (tmp_path / "incoming" / f"{COMMIT_A}.tar.gz").exists()


def test_a_handled_pointer_is_not_shipped_again(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    pointer(fake, COMMIT_A)
    fake.objects[f"bundles/platform/{COMMIT_A}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=1, ship_stderr="vecten agent ship: compose up: failed (exit 1)\n")
    agent.watch(tmp_path / "releases", run, http=fake, sleep=lambda s: None)
    line = agent.watch(tmp_path / "releases", run, http=fake, sleep=lambda s: None)
    assert len(run.ships()) == 1 and "nothing to do" in line


def test_a_pointer_to_the_current_release_reports_it_without_shipping(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    releases = tmp_path / "releases"
    (releases / COMMIT_A).mkdir(parents=True)
    os.symlink(str(releases / COMMIT_A), str(releases / "current"))
    fake = FakeScaleway()
    pointer(fake, COMMIT_A, requested_at="2026-10-18T11:00:00Z")
    run = ShipRun()
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert run.ships() == []
    assert status(fake, COMMIT_A)["ok"] is True
    assert status(fake, COMMIT_A)["requested_at"] == "2026-10-18T11:00:00Z"
    assert f"bundles/platform/{COMMIT_A}.tar.gz" not in [
        unquote(c["url"][len(S3_ROOT) :]) for c in fake.s3_calls()
    ]


def test_a_failing_ship_reports_the_agents_line(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    pointer(fake, COMMIT_B)
    fake.objects[f"bundles/platform/{COMMIT_B}.tar.gz"] = bundle_bytes()
    stderr = "docker noise\nvecten agent ship: compose up: docker compose up failed (exit 1)\n"
    agent.AGENT_COPY.parent.mkdir(parents=True)
    agent.AGENT_COPY.write_text("# agent v1\n")
    agent.watch(
        tmp_path / "releases",
        ShipRun(ship_code=1, ship_stderr=stderr),
        http=fake,
        sleep=lambda s: None,
    )
    assert status(fake, COMMIT_B) == {
        "ok": False,
        "line": "vecten agent ship: compose up: docker compose up failed (exit 1)",
        "requested_at": "2026-10-18T10:00:00Z",
        "running": None,
        "log": [],
    }
    assert agent.AGENT_COPY.read_text() == "# agent v1\n"


def test_watch_reports_the_log_lines_in_the_status(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    pointer(fake, COMMIT_B)
    fake.objects[f"bundles/platform/{COMMIT_B}.tar.gz"] = bundle_bytes()
    stderr = (
        "vecten agent log: mcp-server: Traceback (most recent call last):\n"
        "docker noise\n"
        "vecten agent log: mcp-server: ValueError: provisioner has unexpected grant\n"
        "vecten agent ship: compose up: docker compose up failed (exit 1)\n"
    )
    agent.watch(
        tmp_path / "releases",
        ShipRun(ship_code=1, ship_stderr=stderr),
        http=fake,
        sleep=lambda s: None,
    )
    report = status(fake, COMMIT_B)
    assert report["line"] == "vecten agent ship: compose up: docker compose up failed (exit 1)"
    assert report["log"] == [
        "mcp-server: Traceback (most recent call last):",
        "mcp-server: ValueError: provisioner has unexpected grant",
    ]


def test_a_failed_ship_reports_the_release_the_agent_left_running(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    releases = tmp_path / "releases"
    (releases / COMMIT_A).mkdir(parents=True)
    os.symlink(str(releases / COMMIT_A), str(releases / "current"))
    fake = FakeScaleway()
    pointer(fake, COMMIT_B)
    fake.objects[f"bundles/platform/{COMMIT_B}.tar.gz"] = bundle_bytes()
    stderr = f"vecten agent ship: compose up failed; rolled back to {COMMIT_A}\n"
    agent.watch(releases, ShipRun(ship_code=1, ship_stderr=stderr), http=fake, sleep=lambda s: None)
    report = status(fake, COMMIT_B)
    assert report["ok"] is False and report["running"] == COMMIT_A


def test_a_pointer_to_the_current_release_names_it_running(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    releases = tmp_path / "releases"
    (releases / COMMIT_A).mkdir(parents=True)
    os.symlink(str(releases / COMMIT_A), str(releases / "current"))
    fake = FakeScaleway()
    pointer(fake, COMMIT_A, requested_at="2026-10-18T11:00:00Z")
    agent.watch(releases, ShipRun(), http=fake, sleep=lambda s: None)
    assert status(fake, COMMIT_A)["running"] == COMMIT_A


def _stamp(delta_seconds):
    moment = datetime.now(UTC) + timedelta(seconds=delta_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_without_a_release_a_pointer_older_than_the_new_key_is_skipped(agent, tmp_path):
    watch_config(agent)
    fake = FakeScaleway(metadata_key=f"{ACCESS}:{SECRET}")
    pointer(fake, COMMIT_A, requested_at=_stamp(-3 * 86400))
    fake.objects[f"bundles/platform/{COMMIT_A}.tar.gz"] = bundle_bytes()
    releases = tmp_path / "releases"
    run = ShipRun(ship_code=0)
    line = agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert "predates" in line
    assert run.ships() == [] and status(fake, COMMIT_A) is None
    assert not (agent.STATE_DIR / "watch-handled.json").exists()
    # The pointer the next ship writes comes after the key, and is shipped.
    pointer(fake, COMMIT_A, requested_at=_stamp(60))
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert len(run.ships()) == 1 and status(fake, COMMIT_A)["ok"] is True


def test_a_restamped_pointer_ships_the_release_back_after_a_lost_status(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    releases = tmp_path / "releases"
    fake = FakeScaleway()
    for commit in (COMMIT_A, COMMIT_B):
        fake.objects[f"bundles/platform/{commit}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=0)
    pointer(fake, COMMIT_A, requested_at="2026-10-18T10:00:00Z")
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    # B installs, but its status never reaches the bucket, so A stays recorded as handled.
    pointer(fake, COMMIT_B, requested_at="2026-10-18T11:00:00Z")
    fake.fail("PUT", f"{S3_ROOT}platform/ship-status/", 500)
    with pytest.raises(agent.AgentError):
        agent.watch(releases, run, http=fake, sleep=lambda s: None)
    fake.failures.clear()
    assert (releases / "current").resolve().name == COMMIT_B
    # CI's timeout puts A back. Unchanged, the watcher takes it for handled and keeps B.
    pointer(fake, COMMIT_A, requested_at="2026-10-18T10:00:00Z")
    assert "was handled" in agent.watch(releases, run, http=fake, sleep=lambda s: None)
    # Restamped, it ships A again.
    pointer(fake, COMMIT_A, requested_at="2026-10-18T11:20:00Z", force=False)
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert (releases / "current").resolve().name == COMMIT_A
    assert status(fake, COMMIT_A)["ok"] is True


class _Python310Datetime(datetime):
    """`fromisoformat` as Python 3.9 and 3.10 have it: a trailing `Z` is invalid."""

    @classmethod
    def fromisoformat(cls, text):
        if text.endswith("Z"):
            raise ValueError(f"Invalid isoformat string: {text!r}")
        return super().fromisoformat(text)


def test_a_pointer_older_than_the_key_is_skipped_where_fromisoformat_rejects_z(
    agent, tmp_path, monkeypatch
):
    import datetime as datetime_module

    python310 = types.SimpleNamespace(**{**vars(datetime_module), "datetime": _Python310Datetime})
    monkeypatch.setattr(agent, "datetime", python310)
    watch_config(agent)
    fake = FakeScaleway(metadata_key=f"{ACCESS}:{SECRET}")
    pointer(fake, COMMIT_A, requested_at=_stamp(-3 * 86400))
    fake.objects[f"bundles/platform/{COMMIT_A}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=0)
    line = agent.watch(tmp_path / "releases", run, http=fake, sleep=lambda s: None)
    assert "predates" in line
    assert run.ships() == []


def test_a_host_with_a_release_ships_a_pointer_older_than_its_key(agent, tmp_path):
    watch_config(agent)
    releases = tmp_path / "releases"
    (releases / COMMIT_A).mkdir(parents=True)
    os.symlink(str(releases / COMMIT_A), str(releases / "current"))
    fake = FakeScaleway(metadata_key=f"{ACCESS}:{SECRET}")
    pointer(fake, COMMIT_B, requested_at=_stamp(-3 * 86400))
    fake.objects[f"bundles/platform/{COMMIT_B}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=0)
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert len(run.ships()) == 1


def test_a_missing_bundle_is_reported_naming_its_key(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    pointer(fake, COMMIT_A)
    run = ShipRun()
    agent.watch(tmp_path / "releases", run, http=fake, sleep=lambda s: None)
    report = status(fake, COMMIT_A)
    assert report["ok"] is False and f"bundles/platform/{COMMIT_A}.tar.gz" in report["line"]
    assert run.ships() == []


def test_a_pointer_without_a_commit_id_is_refused(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    pointer(fake, "../../etc")
    with pytest.raises(agent.AgentError, match="pointer"):
        agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)


def test_a_failed_status_write_is_retried(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    pointer(fake, COMMIT_A)
    fake.objects[f"bundles/platform/{COMMIT_A}.tar.gz"] = bundle_bytes()
    fake.fail("PUT", f"{S3_ROOT}platform/ship-status/", 503, times=1)
    slept = []
    agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=slept.append)
    assert status(fake, COMMIT_A)["ok"] is True and len(slept) == 1


def running_release(agent, tmp_path, fake, commit=COMMIT_A):
    """A platform host whose `current` is a staged Scaleway release, its secrets in the store."""
    out = scw_outputs()
    releases = tmp_path / "releases"
    release = stage_scaleway(releases, out, "platform", commit=commit)
    os.symlink(str(release), str(releases / "current"))
    scw_store(fake, out)
    for name in fake_names():
        for secret in fake.secrets.values():
            if secret["name"] == name and not secret["versions"]:
                secret["versions"].append(f"value-of-{name}")
    return releases, release


def fake_names():
    return (
        "AIRFLOW_DB_PASSWORD",
        "LOADER_PASSWORD",
        "AIRFLOW_FERNET_KEY",
        "AIRFLOW_API_SECRET_KEY",
        "AIRFLOW_JWT_SECRET",
        "AIRFLOW_ADMIN_PASSWORD",
        "AIRFLOW_BASIC_AUTH_PASSWORD",
    )


def test_a_rotated_key_recreates_the_running_stack_on_the_new_key(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway(metadata_key=f"{ROTATED_ACCESS}:{ROTATED_SECRET}")
    releases, release = running_release(agent, tmp_path, fake)
    run = ShipRun()
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    (up,) = compose_calls(run)
    assert "--force-recreate" in up["args"]
    assert str(release.resolve() / ".vecten" / "stack") in up["args"]
    assert up["env"]["SCW_ACCESS_KEY"] == ROTATED_ACCESS
    assert up["env"]["SCW_SECRET_KEY"] == ROTATED_SECRET
    assert json.loads(up["env"]["AIRFLOW_CONN_AWS_DEFAULT"])["login"] == ROTATED_ACCESS
    # The new key is acknowledged before the stack is recreated on it.
    ack_index = next(
        i for i, c in enumerate(fake.calls) if c["url"] == S3_ROOT + ack_key(ROTATED_ACCESS)
    )
    assert all(not c["url"].startswith(SCW_API) for c in fake.calls[:ack_index])
    assert not (agent.STATE_DIR / "recreate-pending").exists()
    assert ROTATED_SECRET not in json.dumps([c["args"] for c in run.calls])


def test_an_unchanged_key_recreates_nothing(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway(metadata_key=f"{ACCESS}:{SECRET}")
    releases, _ = running_release(agent, tmp_path, fake)
    run = ShipRun()
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert compose_calls(run) == []


def test_a_failed_recreate_is_retried_on_the_next_run(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway(metadata_key=f"{ROTATED_ACCESS}:{ROTATED_SECRET}")
    releases, _ = running_release(agent, tmp_path, fake)
    fake.fail("GET", f"{SCW_API}/secrets", 503, times=1)
    with pytest.raises(agent.AgentError, match="recreate with the rotated key"):
        agent.watch(releases, ShipRun(), http=fake, sleep=lambda s: None)
    assert (agent.STATE_DIR / "recreate-pending").exists()
    run = ShipRun()
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    (up,) = compose_calls(run)
    assert up["env"]["SCW_ACCESS_KEY"] == ROTATED_ACCESS
    assert not (agent.STATE_DIR / "recreate-pending").exists()


def test_a_failing_recreate_without_a_new_pointer_keeps_the_marker_and_exits_1(
    agent, tmp_path, capsys, monkeypatch
):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway(metadata_key=f"{ROTATED_ACCESS}:{ROTATED_SECRET}")
    releases, _ = running_release(agent, tmp_path, fake)
    pointer(fake, COMMIT_A)
    fake.fail("GET", f"{SCW_API}/secrets", 503)
    monkeypatch.setattr(agent, "http", fake)
    monkeypatch.setattr(agent, "RELEASES", releases)
    assert agent.main(["watch"], run=ShipRun()) == 1
    assert "recreate with the rotated key" in capsys.readouterr().err
    assert (agent.STATE_DIR / "recreate-pending").exists()
    # Nothing claims the pointer was handled while the stack still holds the old key.
    assert status(fake, COMMIT_A) is None
    assert not (agent.STATE_DIR / "watch-handled.json").exists()


def test_a_failing_recreate_never_blocks_a_new_ship(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway(metadata_key=f"{ROTATED_ACCESS}:{ROTATED_SECRET}")
    releases, _ = running_release(agent, tmp_path, fake)
    fake.fail("GET", f"{SCW_API}/secrets", 503)
    with pytest.raises(agent.AgentError, match="recreate with the rotated key"):
        agent.watch(releases, ShipRun(), http=fake, sleep=lambda s: None)
    assert (agent.STATE_DIR / "recreate-pending").exists()
    pointer(fake, COMMIT_B)
    fake.objects[f"bundles/platform/{COMMIT_B}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=0)
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert len(run.ships()) == 1 and compose_calls(run) == []
    assert status(fake, COMMIT_B)["ok"] is True
    assert not (agent.STATE_DIR / "recreate-pending").exists()


def test_a_rotation_with_a_new_pointer_restarts_the_stack_once(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway(metadata_key=f"{ROTATED_ACCESS}:{ROTATED_SECRET}")
    releases, _ = running_release(agent, tmp_path, fake)
    pointer(fake, COMMIT_B)
    fake.objects[f"bundles/platform/{COMMIT_B}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=0)
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert len(run.ships()) == 1 and compose_calls(run) == []
    assert not (agent.STATE_DIR / "recreate-pending").exists()


def test_a_failed_ship_keeps_the_recreate_pending(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway(metadata_key=f"{ROTATED_ACCESS}:{ROTATED_SECRET}")
    releases, _ = running_release(agent, tmp_path, fake)
    pointer(fake, COMMIT_B)
    fake.objects[f"bundles/platform/{COMMIT_B}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=1, ship_stderr="vecten agent ship: compose up: failed (exit 1)\n")
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert status(fake, COMMIT_B)["ok"] is False
    assert (agent.STATE_DIR / "recreate-pending").exists()


def test_the_recreate_marker_is_written_before_the_new_key(agent, tmp_path, monkeypatch):
    watch_config(agent)
    write_key(agent)
    written = []
    original = agent._write_root_only

    def recording(path, text):
        written.append(path.name)
        original(path, text)

    monkeypatch.setattr(agent, "_write_root_only", recording)
    fake = FakeScaleway(metadata_key=f"{ROTATED_ACCESS}:{ROTATED_SECRET}")
    agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    assert written.index("recreate-pending") < written.index("scw-key")


def test_a_first_key_without_a_release_recreates_nothing(agent, tmp_path):
    watch_config(agent)
    fake = FakeScaleway(metadata_key=f"{ACCESS}:{SECRET}")
    run = ShipRun()
    agent.watch(tmp_path / "releases", run, http=fake, sleep=lambda s: None)
    assert compose_calls(run) == []
    assert not (agent.STATE_DIR / "recreate-pending").exists()


def test_a_forced_pointer_reships_the_current_release_over_itself(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    releases, release = running_release(agent, tmp_path, fake)
    (release / "kept-by-extract-over").write_text("x")
    pointer(fake, COMMIT_A, requested_at="2026-10-18T12:00:00Z", force=True)
    fake.objects[f"bundles/platform/{COMMIT_A}.tar.gz"] = bundle_bytes()
    run = ShipRun(ship_code=0)
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    (ship_args,) = run.ships()
    assert ship_args[-2:] == ["--release", str(release)]
    # The running release is extracted over, never removed, as on AWS; ship then sees a
    # re-ship of `current` and recreates its containers.
    assert (release / "kept-by-extract-over").is_file()
    assert os.readlink(releases / "current") == str(release)
    assert status(fake, COMMIT_A) == {
        "ok": True,
        "line": f"shipped {COMMIT_A}",
        "requested_at": "2026-10-18T12:00:00Z",
        "running": COMMIT_A,
        "log": [],
    }


def test_a_rollback_pointer_to_the_current_release_without_force_is_not_reshipped(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    releases, _ = running_release(agent, tmp_path, fake)
    pointer(fake, COMMIT_A, requested_at="2026-10-18T12:00:00Z", force=False)
    run = ShipRun()
    agent.watch(releases, run, http=fake, sleep=lambda s: None)
    assert run.ships() == [] and compose_calls(run) == []
    assert status(fake, COMMIT_A)["line"] == f"{COMMIT_A} is already the current release"


def test_a_refused_privileged_port_names_root(monkeypatch):
    agent = load_agent()

    class Refused:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            raise PermissionError(errno.EACCES, "Permission denied")

        def close(self):
            pass

    monkeypatch.setattr(
        agent,
        "http_client",
        types.SimpleNamespace(HTTPConnection=Refused, HTTPException=http.client.HTTPException),
    )
    with pytest.raises(agent.PrivilegeError, match="must run as root"):
        agent.http("GET", agent.METADATA_KEY_URL, privileged=True)


def test_watch_without_root_fails_naming_root_instead_of_waiting(agent, capsys, monkeypatch):
    watch_config(agent)

    def refused(method, url, headers=None, body=None, privileged=False):
        raise agent.PrivilegeError("binding a source port below 1024 was refused; must run as root")

    monkeypatch.setattr(agent, "http", refused)
    assert agent.main(["watch"], run=ShipRun()) == 1
    captured = capsys.readouterr()
    (line,) = captured.err.strip().splitlines()
    assert "must run as root" in line and "waiting" not in captured.out


def test_a_concurrent_run_is_blocked_by_the_lock(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    agent.RUN_DIR.mkdir(parents=True)
    fake = FakeScaleway()
    with open(agent.RUN_DIR / "watch.lock", "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        line = agent.watch(tmp_path / "releases", ShipRun(), http=fake, sleep=lambda s: None)
    assert "another watch run" in line and fake.calls == []


def test_main_watch_prints_one_line_and_exits_zero(agent, tmp_path, capsys, monkeypatch):
    watch_config(agent)
    monkeypatch.setattr(agent, "http", FakeScaleway())
    monkeypatch.setattr(agent, "RELEASES", tmp_path / "releases")
    assert agent.main(["watch"], run=ShipRun()) == 0
    (line,) = capsys.readouterr().out.strip().splitlines()
    assert line.startswith("vecten agent watch: ") and "waiting for key" in line


def test_main_watch_without_a_config_fails_naming_it(agent, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(agent, "http", FakeScaleway())
    assert agent.main(["watch"], run=ShipRun()) == 1
    (line,) = capsys.readouterr().err.strip().splitlines()
    assert "watch.json" in line


def test_cli_offers_watch():
    r = subprocess.run(
        [sys.executable, str(APPLY), "--help"], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0 and "watch" in r.stdout


# ---------------------------------------------------------------- the HTTP seam


class _Server:
    def __init__(self, handler):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def recording_handler(status, body=b"", location=None, seen=None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if seen is not None:
                seen.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "port": self.client_address[1],
                    }
                )
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    return Handler


@pytest.fixture
def no_proxy(monkeypatch):
    for name in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def test_http_returns_the_status_and_body(no_proxy):
    agent = load_agent()
    server = _Server(recording_handler(404, b'{"type": "not_found"}'))
    try:
        assert agent.http("GET", f"{server.url}/x", {"X-Auth-Token": "t"}) == (
            404,
            b'{"type": "not_found"}',
        )
    finally:
        server.close()


def test_http_never_follows_a_redirect_with_the_token(no_proxy):
    agent = load_agent()
    landed = []
    target = _Server(recording_handler(200, b"stolen", seen=landed))
    origin = _Server(recording_handler(302, location=f"{target.url}/elsewhere"))
    try:
        status, _ = agent.http("GET", f"{origin.url}/secrets", {"X-Auth-Token": "token-value"})
    finally:
        origin.close()
        target.close()
    assert status == 302 and landed == []


def test_http_reports_a_protocol_error_by_type_only(no_proxy):
    agent = load_agent()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def garbage():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b"NOT HTTP secret-ish\r\n\r\n")
        conn.close()

    thread = threading.Thread(target=garbage, daemon=True)
    thread.start()
    try:
        with pytest.raises(agent.AgentError) as exc:
            agent.http("GET", f"http://127.0.0.1:{port}/x", {})
    finally:
        listener.close()
    assert "BadStatusLine" in str(exc.value) and "secret-ish" not in str(exc.value)


def test_http_reports_an_unreachable_host_by_type_only(no_proxy):
    agent = load_agent()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    with pytest.raises(agent.AgentError) as exc:
        agent.http("GET", f"http://127.0.0.1:{port}/x?token=abc", {})
    assert "abc" not in str(exc.value) and "Error" in str(exc.value)


def test_a_privileged_request_binds_a_source_port_from_the_privileged_range(no_proxy, monkeypatch):
    agent = load_agent()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    base = free.getsockname()[1]
    free.close()
    monkeypatch.setattr(agent, "PRIVILEGED_PORTS", range(base, base + 1))
    seen = []
    server = _Server(recording_handler(200, b"AK:SK", seen=seen))
    try:
        assert agent.http("GET", f"{server.url}/user_data/vecten-key", privileged=True) == (
            200,
            b"AK:SK",
        )
    finally:
        server.close()
    assert seen[0]["port"] == base and seen[0]["path"] == "/user_data/vecten-key"


def test_the_watcher_reports_the_volumes_its_release_agent_named(agent, tmp_path):
    watch_config(agent)
    write_key(agent)
    fake = FakeScaleway()
    pointer(fake, COMMIT_A)
    fake.objects[f"bundles/platform/{COMMIT_A}.tar.gz"] = bundle_bytes()
    stderr = "vecten agent volume: p_svc-old-data\nother line\n"
    agent.watch(
        tmp_path / "releases", ShipRun(ship_code=0, ship_stderr=stderr), http=fake,
        sleep=lambda s: None,
    )  # fmt: skip
    assert status(fake, COMMIT_A)["volumes"] == ["p_svc-old-data"]
