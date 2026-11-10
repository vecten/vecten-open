import http.server
import importlib.util
import io
import json
import socket
import sys
import threading
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import LogTarget, get_provider, scope_for
from vecten_shared.provider import scaleway as scaleway_module
from vecten_shared.provider.aws import AwsRuntimeProvider
from vecten_shared.provider.scaleway import (
    RUNTIME_WRITABLE,
    ScalewayRuntimeProvider,
    scaleway_resource_name,
    scaleway_secret_path,
)


def _load_fixtures():
    # Every package's test directory is a package named `tests`, so a relative import
    # resolves against whichever one pytest imported first; load the helper by path.
    name = "vecten_shared_tests_provider_fixtures"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("provider_fixtures.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


fixtures = _load_fixtures()
BUCKET = fixtures.BUCKET
SCW_API = fixtures.SCW_API
SCW_PROJECT_ID = fixtures.SCW_PROJECT_ID
SCW_SECRET_KEY = fixtures.SCW_SECRET_KEY
FakeSecretManager = fixtures.FakeSecretManager
scaleway_credentials = fixtures.scaleway_credentials
scaleway_outputs = fixtures.scaleway_outputs
write_scaleway_project = fixtures.write_scaleway_project
aws_credentials = fixtures.aws_credentials

PLATFORM = "/vecten/acme/prod/platform"
MCP = "/vecten/acme/prod/mcp"


@pytest.fixture
def root(tmp_path, monkeypatch):
    scaleway_credentials(monkeypatch)
    write_scaleway_project(tmp_path)
    return tmp_path


@pytest.fixture
def store(monkeypatch):
    return FakeSecretManager().install(monkeypatch)


def provider(root) -> ScalewayRuntimeProvider:
    return ScalewayRuntimeProvider(root, "prod")


def test_secret_path_and_resource_name_follow_the_scope():
    assert scaleway_secret_path("acme", "prod", "platform") == PLATFORM
    assert scaleway_resource_name("acme", "prod", "HUBSPOT_API_KEY") == (
        "/vecten/acme/prod/platform/HUBSPOT_API_KEY"
    )
    assert scaleway_resource_name("acme", "prod", "MCP_PROVISIONER_PASSWORD") == (
        "/vecten/acme/prod/mcp/MCP_PROVISIONER_PASSWORD"
    )
    assert scaleway_resource_name("acme", "prod", "SETUP_STATE_ADMIN_PASSWORD") == (
        "/vecten/acme/prod/setup/SETUP_STATE_ADMIN_PASSWORD"
    )


def test_resource_name_refuses_a_name_outside_the_rule():
    with pytest.raises(Failure) as exc:
        scaleway_resource_name("acme", "prod", "../MCP_X")
    assert exc.value.code == ExitCode.USAGE


def test_setup_names_have_their_own_scope():
    assert scope_for("SETUP_STATE_ADMIN_PASSWORD") == "setup"
    assert scope_for("SETUP_WAREHOUSE_ADMIN_PASSWORD") == "setup"
    assert scope_for("SETUPX") == "platform"


def test_get_secret_finds_the_secret_by_path_and_name_then_reads_it_by_id(root, store):
    store.add(PLATFORM, "HUBSPOT_API_KEY_OLD", "stale")
    sid = store.add(PLATFORM, "HUBSPOT_API_KEY", "v1", "hub-value")
    assert provider(root).get_secret("HUBSPOT_API_KEY") == "hub-value"
    (list_method, list_url), (access_method, access_url) = store.calls
    assert list_method == "GET"
    parts = urlsplit(list_url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{SCW_API}/secrets"
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert query["project_id"] == SCW_PROJECT_ID
    assert query["path"] == PLATFORM and query["name"] == "HUBSPOT_API_KEY"
    assert (access_method, access_url) == (
        "GET",
        f"{SCW_API}/secrets/{sid}/versions/latest_enabled/access",
    )


def test_get_secret_reads_the_mcp_and_setup_scopes(root, store):
    store.add(MCP, "MCP_PROVISIONER_PASSWORD", "prov")
    store.add("/vecten/acme/prod/setup", "SETUP_STATE_ADMIN_PASSWORD", "adm")
    store.add(PLATFORM, "MCP_PROVISIONER_PASSWORD", "wrong-scope")
    p = provider(root)
    assert p.get_secret("MCP_PROVISIONER_PASSWORD") == "prov"
    assert p.get_secret("SETUP_STATE_ADMIN_PASSWORD") == "adm"


def test_get_secret_ignores_another_projects_secret(root, store):
    store.add(
        PLATFORM, "HUBSPOT_API_KEY", "other", project_id="99999999-0000-0000-0000-000000000000"
    )
    with pytest.raises(Failure) as exc:
        provider(root).get_secret("HUBSPOT_API_KEY")
    assert exc.value.code == ExitCode.PREREQUISITE


def test_missing_secret_names_secret_set_for_the_environment(root, store):
    with pytest.raises(Failure) as exc:
        provider(root).get_secret("HUBSPOT_API_KEY")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "vecten secret set HUBSPOT_API_KEY --env prod" in exc.value.message


def test_a_secret_with_no_version_names_secret_set(root, store):
    store.add(MCP, "MCP_IMPERSONATOR_PASSWORD")
    with pytest.raises(Failure) as exc:
        provider(root).get_secret("MCP_IMPERSONATOR_PASSWORD")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "vecten secret set MCP_IMPERSONATOR_PASSWORD --env prod" in exc.value.message


def test_runtime_writes_only_the_impersonator_password(root, store):
    assert RUNTIME_WRITABLE == frozenset({"MCP_IMPERSONATOR_PASSWORD", "MCP_SIGNING_KEY"})
    store.add(PLATFORM, "HUBSPOT_API_KEY")
    with pytest.raises(Failure) as exc:
        provider(root).put_secret("HUBSPOT_API_KEY", "nope-value")
    assert exc.value.code == ExitCode.REFUSED
    assert "vecten CLI" in exc.value.message and "nope-value" not in exc.value.message
    assert store.calls == []


def test_impersonator_password_adds_a_version_to_its_shell(root, store):
    sid = store.add(MCP, "MCP_IMPERSONATOR_PASSWORD")
    p = provider(root)
    p.put_secret("MCP_IMPERSONATOR_PASSWORD", "v1")
    p.put_secret("MCP_IMPERSONATOR_PASSWORD", "v2")
    assert store.value(MCP, "MCP_IMPERSONATOR_PASSWORD") == "v2"
    assert ("POST", f"{SCW_API}/secrets/{sid}/versions") in store.calls
    assert p.get_secret("MCP_IMPERSONATOR_PASSWORD") == "v2"


def test_writing_without_the_terraform_shell_names_deploy(root, store):
    with pytest.raises(Failure) as exc:
        provider(root).put_secret("MCP_IMPERSONATOR_PASSWORD", "s3cr3t")
    assert exc.value.code == ExitCode.PREREQUISITE
    assert "vecten deploy --env prod" in exc.value.message
    assert "s3cr3t" not in exc.value.message
    assert all(method == "GET" for method, _ in store.calls)


def test_list_secrets_lists_names_under_the_environment_paths(root, store):
    store.add(PLATFORM, "STRIPE_KEY", "x")
    store.add(MCP, "MCP_STATE_DB_PASSWORD", "x")
    store.add(PLATFORM, "HUBSPOT_API_KEY")
    store.add("/vecten/acme/prod/setup", "SETUP_STATE_ADMIN_PASSWORD")
    store.add(MCP, "HUBSPOT_API_KEY", "wrong scope")
    store.add("/vecten/acme/staging/platform", "OTHER_ENV_KEY")
    store.add("/vecten/acmecorp/prod/platform", "OTHER_PROJECT_KEY")
    store.add("/vecten/acme/prod/deploy", "HOST_KEY_MCP")
    store.add("/", "UNRELATED")
    store.add(PLATFORM, "ELSEWHERE", project_id="99999999-0000-0000-0000-000000000000")
    assert provider(root).list_secrets() == [
        "HUBSPOT_API_KEY",
        "MCP_STATE_DB_PASSWORD",
        "SETUP_STATE_ADMIN_PASSWORD",
        "STRIPE_KEY",
    ]


def test_list_secrets_reads_every_page(root, store):
    for i in range(130):
        store.add(PLATFORM, f"KEY_{i:03d}")
    names = provider(root).list_secrets()
    assert len(names) == 130 and names[0] == "KEY_000" and names[-1] == "KEY_129"
    assert len(store.calls) >= 2


@pytest.mark.parametrize(
    ("call", "operation"),
    [
        (lambda p: p.get_secret("HUBSPOT_API_KEY"), "ListSecrets"),
        (lambda p: p.list_secrets(), "ListSecrets"),
    ],
)
def test_an_error_names_the_status_and_type_only(root, store, call, operation):
    store.failure = (
        500,
        json.dumps({"type": "internal_error", "message": "the value is s3cr3t"}).encode(),
    )
    with pytest.raises(Failure) as exc:
        call(provider(root))
    assert exc.value.code == ExitCode.TOOL_FAILED
    assert exc.value.message == f"Secret Manager {operation} failed: HTTP 500 internal_error"
    assert exc.value.__cause__ is None


def test_access_and_write_errors_name_their_operation(root, store, monkeypatch):
    store.add(MCP, "MCP_IMPERSONATOR_PASSWORD", "old")
    real = store.__call__

    def failing(method, url, headers, body):
        if "/versions" in url:
            return 403, json.dumps(
                {"type": "permissions_denied", "message": "denied for s3cr3t"}
            ).encode()
        return real(method, url, headers, body)

    monkeypatch.setattr(scaleway_module, "_http", failing)
    p = provider(root)
    with pytest.raises(Failure) as exc:
        p.get_secret("MCP_IMPERSONATOR_PASSWORD")
    assert exc.value.message == (
        "Secret Manager AccessSecretVersion failed: HTTP 403 permissions_denied"
    )
    with pytest.raises(Failure) as exc:
        p.put_secret("MCP_IMPERSONATOR_PASSWORD", "s3cr3t")
    assert exc.value.message == (
        "Secret Manager CreateSecretVersion failed: HTTP 403 permissions_denied"
    )


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"type": "bad type: s3cr3t", "message": "s3cr3t"}).encode(),
        json.dumps({"message": "s3cr3t"}).encode(),
        json.dumps(["s3cr3t"]).encode(),
        b"<html>s3cr3t</html>",
    ],
    ids=["type-with-spaces", "no-type", "not-an-object", "not-json"],
)
def test_an_error_without_a_plain_type_names_the_status_only(root, store, body):
    store.failure = (502, body)
    with pytest.raises(Failure) as exc:
        provider(root).list_secrets()
    assert exc.value.message == "Secret Manager ListSecrets failed: HTTP 502"


def test_a_malformed_success_body_is_a_tool_failure(root, store):
    store.failure = (200, b"<html>s3cr3t</html>")
    with pytest.raises(Failure) as exc:
        provider(root).list_secrets()
    assert exc.value.code == ExitCode.TOOL_FAILED
    assert "s3cr3t" not in exc.value.message and "ListSecrets" in exc.value.message


def test_missing_secret_key_names_the_variable(root, store, monkeypatch):
    monkeypatch.delenv("SCW_SECRET_KEY")
    with pytest.raises(Failure) as exc:
        provider(root).get_secret("HUBSPOT_API_KEY")
    assert exc.value.code == ExitCode.PREREQUISITE and "SCW_SECRET_KEY" in exc.value.message
    assert store.calls == []


def test_project_id_comes_from_the_environment_when_vecten_toml_has_none(
    tmp_path, monkeypatch, store
):
    scaleway_credentials(monkeypatch)
    write_scaleway_project(tmp_path, project_id=None)
    with pytest.raises(Failure) as exc:
        provider(tmp_path).list_secrets()
    assert exc.value.code == ExitCode.CONFIG
    assert 'cloud_ids["project"]' in exc.value.message
    assert "SCW_DEFAULT_PROJECT_ID" in exc.value.message
    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", SCW_PROJECT_ID)
    store.add(PLATFORM, "HUBSPOT_API_KEY", "x")
    assert provider(tmp_path).list_secrets() == ["HUBSPOT_API_KEY"]


def test_vecten_toml_project_id_wins_over_the_environment(root, store, monkeypatch):
    monkeypatch.setenv("SCW_DEFAULT_PROJECT_ID", "99999999-0000-0000-0000-000000000000")
    store.add(PLATFORM, "HUBSPOT_API_KEY", "x")
    assert provider(root).list_secrets() == ["HUBSPOT_API_KEY"]


def test_region_comes_from_the_outputs(tmp_path, monkeypatch):
    scaleway_credentials(monkeypatch)
    write_scaleway_project(tmp_path, outputs={**scaleway_outputs(), "region": "nl-ams"})
    seen = []

    def record(method, url, headers, body):
        seen.append(url)
        return 200, json.dumps({"secrets": [], "total_count": 0}).encode()

    monkeypatch.setattr(scaleway_module, "_http", record)
    provider(tmp_path).list_secrets()
    assert seen[0].startswith(
        "https://api.scaleway.com/secret-manager/v1beta1/regions/nl-ams/secrets?"
    )


@pytest.mark.parametrize("db", ["warehouse", "airflow", "mcp_state"])
def test_connections_equal_the_aws_ones_for_the_same_outputs(root, monkeypatch, db):
    aws_credentials(monkeypatch, root)
    assert provider(root).connection("prod", db) == AwsRuntimeProvider(root, "prod").connection(
        "prod", db
    )


def test_log_target_is_the_platform_logs_prefix(root, store):
    assert provider(root).log_target("prod") == LogTarget("bucket", f"s3://{BUCKET}/platform/logs")
    assert store.calls == []


@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("bootstrap", ("prod", "acme/data")),
        ("provision", ("prod",)),
        ("destroy", ("prod",)),
        ("ship", ("prod", "platform", "/tmp/project")),
    ],
)
def test_control_plane_operations_are_refused_at_runtime(root, operation, args):
    with pytest.raises(Failure) as exc:
        getattr(provider(root), operation)(*args)
    assert exc.value.code == ExitCode.REFUSED
    assert operation in exc.value.message and "vecten CLI" in exc.value.message


def test_get_provider_returns_the_scaleway_runtime_class(root):
    p = get_provider("scaleway", root, "prod")
    assert isinstance(p, ScalewayRuntimeProvider) and p.env == "prod"


def test_constructing_the_provider_reads_nothing(tmp_path, store):
    p = ScalewayRuntimeProvider(tmp_path / "absent", "prod")
    assert p.env == "prod" and store.calls == []


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_http_sends_the_request_and_returns_status_and_body(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen.update(
            method=request.get_method(),
            url=request.full_url,
            token=request.get_header("X-auth-token"),
            body=request.data,
            timeout=timeout,
        )
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(scaleway_module, "_open", urlopen)
    status, body = scaleway_module._http(
        "POST", f"{SCW_API}/secrets/x/versions", {"X-Auth-Token": SCW_SECRET_KEY}, b"{}"
    )
    assert (status, body) == (200, b'{"ok": true}')
    assert seen["method"] == "POST" and seen["token"] == SCW_SECRET_KEY
    assert seen["body"] == b"{}" and seen["timeout"] > 0


def test_http_returns_an_error_status_with_its_body(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"type": "not_found"}')
        )

    monkeypatch.setattr(scaleway_module, "_open", urlopen)
    assert scaleway_module._http("GET", f"{SCW_API}/secrets", {}, None) == (
        404,
        b'{"type": "not_found"}',
    )


def test_an_unreachable_api_names_the_error_type_only(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.URLError("s3cr3t.example.invalid")

    monkeypatch.setattr(scaleway_module, "_open", urlopen)
    with pytest.raises(Failure) as exc:
        scaleway_module._http("GET", f"{SCW_API}/secrets", {}, None)
    assert exc.value.code == ExitCode.TOOL_FAILED
    assert exc.value.message == "Secret Manager unreachable: URLError"


@pytest.fixture
def no_proxy(monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def _serve(status, body=b"", location=None, seen=None):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if seen is not None:
                seen.append(self.path)
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_http_never_follows_a_redirect_with_the_token(no_proxy):
    landed = []
    target, target_url = _serve(200, b"{}", seen=landed)
    origin, origin_url = _serve(302, location=f"{target_url}/elsewhere")
    try:
        status, _ = scaleway_module._http(
            "GET", f"{origin_url}/secrets", {"X-Auth-Token": SCW_SECRET_KEY}, None
        )
    finally:
        for server in (origin, target):
            server.shutdown()
            server.server_close()
    assert status == 302 and landed == []


def test_a_protocol_error_names_its_type_only(no_proxy):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def garbage():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(b"NOT HTTP secret-ish\r\n\r\n")
        conn.close()

    threading.Thread(target=garbage, daemon=True).start()
    try:
        with pytest.raises(Failure) as exc:
            scaleway_module._http("GET", f"http://127.0.0.1:{port}/secrets", {}, None)
    finally:
        listener.close()
    assert exc.value.message == "Secret Manager unreachable: BadStatusLine"
