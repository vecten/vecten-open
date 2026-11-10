import json
import stat
import time

import httpx
import pytest

from vecten.mcp_client import (
    TokenCache,
    device_complete,
    device_start,
    request_admin,
    session_usable,
)
from vecten_shared.config import load_config, save_config
from vecten_shared.output import ExitCode, Failure
from vecten_shared.records import read_record

from .fixture_project import render_fixture


def test_cache_private_and_outside_project(tmp_path):
    cache = TokenCache(tmp_path / "home" / "tokens.json")
    cache.save("https://mcp.example", {"access_token": "fixture"})
    assert stat.S_IMODE(cache.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache.path.parent.stat().st_mode) == 0o700
    assert cache.load("https://mcp.example")["access_token"] == "fixture"
    cache.remove("https://mcp.example")
    assert cache.load("https://mcp.example") is None


def test_device_login_uses_single_public_document_and_private_pending_state(tmp_path):
    cache = TokenCache(tmp_path / "tokens.json")
    requests = []

    def handle(req):
        requests.append(req)
        if req.url.path == "/register":
            return httpx.Response(201, json={"client_id": "cli"})
        if req.url.path == "/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "hidden",
                    "user_code": "PUBLIC",
                    "verification_uri": "https://mcp.example/device/verify",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if req.url.path == "/token":
            return httpx.Response(
                200, json={"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        public = device_start("https://mcp.example", http, cache)
        assert "hidden" not in json.dumps(public) and public["user_code"] == "PUBLIC"
        device_complete("https://mcp.example", http, cache, sleep=lambda _: None)
    assert cache.load("https://mcp.example")["access_token"] == "access"
    assert b"device_code=hidden" in requests[-1].content


def test_missing_token_names_login_before_network(tmp_path, monkeypatch):
    from vecten.mcp_client import request_admin

    with pytest.raises(Failure, match="vecten login"):
        request_admin("https://mcp.example", "users", cache=TokenCache(tmp_path / "tokens.json"))


def test_admin_requests_carry_token_and_refresh_privately(tmp_path):
    from vecten.mcp_client import request_admin

    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(
        "https://mcp.example",
        {"access_token": "old", "refresh_token": "refresh", "client_id": "cli", "expires_at": 0},
    )
    seen = []

    def handle(req):
        seen.append(req)
        if req.url.path == "/token":
            return httpx.Response(
                200, json={"access_token": "fresh", "refresh_token": "next", "expires_in": 3600}
            )
        assert req.headers["authorization"] == "Bearer fresh"
        return httpx.Response(200, json={"ok": True, "data": []})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        assert request_admin("https://mcp.example", "users", cache=cache, http=http) == []
    assert len(seen) == 2 and cache.load("https://mcp.example")["refresh_token"] == "next"


def test_an_admin_read_sends_its_parameters_in_the_query(tmp_path):
    from vecten.mcp_client import request_admin

    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(
        "https://mcp.example",
        {"access_token": "t", "refresh_token": "r", "client_id": "cli", "expires_at": 1e12},
    )
    seen = []

    def handle(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True, "data": []})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        request_admin(
            "https://mcp.example", "gaps", params={"status": "all"}, cache=cache, http=http
        )
    (sent,) = seen
    assert sent.method == "GET" and sent.url.path == "/api/admin/gaps"
    assert sent.url.params["status"] == "all"


def test_a_refused_admin_request_names_the_servers_reason(tmp_path):
    from vecten.mcp_client import request_admin

    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(
        "https://mcp.example",
        {"access_token": "t", "refresh_token": "r", "client_id": "cli", "expires_at": 1e12},
    )
    reason = "administration failed: the warehouse refused the request (100132): not a role"

    def handle(req):
        return httpx.Response(503, json={"ok": False, "error": reason})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(Failure) as raised:
            request_admin("https://mcp.example", "grant", {}, cache=cache, http=http)
    assert raised.value.message == f"MCP refused grant (HTTP 503): {reason}"


def test_browser_login_validates_loopback_and_pkce(tmp_path):
    import threading
    from urllib.parse import parse_qs, urlparse

    from vecten.mcp_login import loopback_login

    cache = TokenCache(tmp_path / "tokens.json")
    observed = {}

    def handle(req):
        if req.url.path == "/register":
            observed.update(json.loads(req.content))
            return httpx.Response(201, json={"client_id": "cli"})
        assert b"code_verifier=" in req.content and b"code=fixture-code" in req.content
        return httpx.Response(
            200, json={"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
        )

    threads = []

    def open_browser(url):
        query = parse_qs(urlparse(url).query)
        assert query["code_challenge_method"] == ["S256"]
        target = query["redirect_uri"][0] + "?code=fixture-code&state=" + query["state"][0]
        thread = threading.Thread(target=lambda: httpx.get(target, trust_env=False))
        thread.start()
        threads.append(thread)
        return True

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        result = loopback_login("https://mcp.example", http, cache, open_browser=open_browser)
    for thread in threads:
        thread.join(timeout=5)
    assert result["authenticated"] and observed["redirect_uris"][0].startswith("http://127.0.0.1:")


def test_concurrent_admin_processes_refresh_only_once(tmp_path):
    import subprocess
    import sys
    import time

    issuer = "https://mcp.example"
    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(
        issuer,
        {"access_token": "old", "refresh_token": "refresh", "client_id": "cli", "expires_at": 0},
    )
    worker = tmp_path / "worker.py"
    worker.write_text("""
import sys, time
from pathlib import Path
import httpx
from vecten.mcp_client import TokenCache, request_admin
root, number = Path(sys.argv[1]), sys.argv[2]
(root / ("ready-" + number)).touch()
deadline = time.monotonic() + 10
while not (root / "start").exists():
    if time.monotonic() > deadline:
        raise TimeoutError("worker was not started")
    time.sleep(0.01)
def handle(request):
    if request.url.path == "/token":
        with (root / "refreshes").open("a") as stream:
            stream.write("refresh\\n")
        time.sleep(0.3)
        return httpx.Response(200, json={
            "access_token": "fresh", "refresh_token": "next", "expires_in": 3600})
    assert request.headers["authorization"] == "Bearer fresh"
    return httpx.Response(200, json={"ok": True, "data": []})
with httpx.Client(transport=httpx.MockTransport(handle)) as http:
    assert request_admin("https://mcp.example", "users",
        cache=TokenCache(root / "tokens.json"), http=http) == []
""")
    processes = [
        subprocess.Popen(
            [sys.executable, str(worker), str(tmp_path), str(i)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(2)
    ]
    try:
        deadline = time.monotonic() + 10
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline, "workers did not start"
            time.sleep(0.01)
        (tmp_path / "start").touch()
        for process in processes:
            output = process.communicate(timeout=20)
            assert process.returncode == 0, output
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert (tmp_path / "refreshes").read_text().splitlines() == ["refresh"]
    assert cache.load(issuer)["refresh_token"] == "next"


@pytest.mark.parametrize("interruption", ["response_lost", "save_interrupted"])
def test_uncertain_refresh_requires_login_without_replaying(tmp_path, monkeypatch, interruption):
    from vecten.mcp_client import request_admin

    issuer = "https://mcp.example"
    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(
        issuer,
        {"access_token": "old", "refresh_token": "refresh", "client_id": "cli", "expires_at": 0},
    )
    refreshes = []
    write = cache._write

    def interrupted_write(data):
        if data[issuer].get("refresh_token") == "next":
            raise KeyboardInterrupt()
        write(data)

    if interruption == "save_interrupted":
        monkeypatch.setattr(cache, "_write", interrupted_write)

    def handle(request):
        assert request.url.path == "/token"
        refreshes.append(request)
        if interruption == "response_lost":
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(
            200, json={"access_token": "fresh", "refresh_token": "next", "expires_in": 3600}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises((Failure, KeyboardInterrupt)):
            request_admin(issuer, "users", cache=cache, http=http)
        with pytest.raises(Failure, match="vecten login"):
            request_admin(issuer, "users", cache=TokenCache(cache.path), http=http)
    assert len(refreshes) == 1


NOW = 1_000_000.0


@pytest.mark.parametrize(
    ("entry", "usable"),
    [
        (None, False),
        ({"access_token": "a", "expires_at": NOW + 3600}, True),
        ({"access_token": "a", "expires_at": NOW + 10}, False),
        ({"access_token": "a", "expires_at": NOW - 1, "refresh_token": "r"}, True),
        ({"access_token": "a", "expires_at": NOW - 1}, False),
        ({"access_token": "a"}, False),
        (
            {
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": NOW + 3600,
                "refresh_pending": True,
            },
            False,
        ),
        ({"pending": {"device_code": "d"}, "client_id": "c", "deadline": NOW + 600}, False),
    ],
)
def test_session_usable_over_entry_shapes(entry, usable):
    assert session_usable(entry, NOW) is usable


ISSUER = "https://mcp.example"


def _session(**changes):
    return {
        "access_token": "a-token",
        "refresh_token": "r-token",
        "client_id": "cli-1",
        "expires_at": time.time() + 3600,
        **changes,
    }


def test_a_rejected_session_is_dropped_from_the_cache(tmp_path):
    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(ISSUER, _session())
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(Failure) as caught:
        request_admin(ISSUER, "users", cache=cache, http=http)
    assert caught.value.code == ExitCode.PREREQUISITE
    assert "vecten login" in caught.value.message
    assert cache.load(ISSUER) is None


def test_a_401_keeps_a_session_another_process_saved(tmp_path):
    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(ISSUER, _session())

    def handle(request):
        cache.save(ISSUER, _session(access_token="other"))
        return httpx.Response(401, json={})

    with pytest.raises(Failure):
        request_admin(
            ISSUER, "users", cache=cache, http=httpx.Client(transport=httpx.MockTransport(handle))
        )
    assert cache.load(ISSUER)["access_token"] == "other"


def test_a_refused_refresh_drops_the_session(tmp_path):
    cache = TokenCache(tmp_path / "tokens.json")
    cache.save(ISSUER, _session(expires_at=0))
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid"}))
    )
    with pytest.raises(Failure) as caught:
        request_admin(ISSUER, "users", cache=cache, http=http)
    assert caught.value.code == ExitCode.PREREQUISITE
    assert caught.value.message == "session expired; run vecten login"
    assert cache.load(ISSUER) is None


LOGIN_ISSUER = "https://mcp.acme.test"


@pytest.fixture
def logged_in_project(tmp_path, monkeypatch):
    """A deploy project whose login completes against a stand-in MCP, saving a session."""
    root = render_fixture(tmp_path, "postgres", deploy=True, domain="acme.test")
    config = load_config(root)
    config.environments["prod"].bootstrapped = True
    save_config(root, config)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(root)

    def finish(issuer, http, cache, **_):
        cache.save(issuer, _session())
        return {"authenticated": True, "issuer": issuer}

    monkeypatch.setattr("vecten.mcp_client.device_complete", finish)
    monkeypatch.setattr("vecten.mcp_login.loopback_login", finish)
    monkeypatch.setattr(
        "vecten.mcp_client.http_client",
        lambda *a, **k: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        ),
    )
    return root


@pytest.mark.parametrize("flag", ["--complete", "--browser"])
def test_a_completed_login_records_the_session_without_a_token(invoke, logged_in_project, flag):
    r = invoke("login", "--env", "prod", flag, "--json")
    assert r.exit_code == 0, r.stdout
    assert read_record(logged_in_project, "login/prod")["client_id"] == "cli-1"
    text = (logged_in_project / ".vecten" / "records" / "login" / "prod.json").read_text()
    assert "a-token" not in text and "r-token" not in text


def test_logout_removes_the_login_record(invoke, logged_in_project):
    assert invoke("login", "--env", "prod", "--complete", "--json").exit_code == 0
    r = invoke("logout", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert read_record(logged_in_project, "login/prod") is None


def test_a_started_device_login_records_nothing(invoke, logged_in_project, monkeypatch):
    device = {"user_code": "U", "verification_uri": LOGIN_ISSUER + "/device/verify"}
    monkeypatch.setattr("vecten.mcp_client.device_start", lambda issuer, http, cache: dict(device))
    r = invoke("login", "--env", "prod", "--json")
    assert r.exit_code == 0, r.stdout
    assert read_record(logged_in_project, "login/prod") is None
