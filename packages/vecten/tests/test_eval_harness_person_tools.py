"""The person's tools in a run: the MCP client standing for their AI assistant and the browser
steps for their sign-ins."""

import asyncio
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthToken

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from eval_harness import browser, cases, mcp_client, runs  # noqa: E402


def test_file_storage_round_trips_with_private_files(tmp_path):
    store = mcp_client.FileStorage(tmp_path / "state")
    assert asyncio.run(store.get_tokens()) is None
    asyncio.run(store.set_tokens(OAuthToken(access_token="a", token_type="Bearer")))
    assert asyncio.run(store.get_tokens()).access_token == "a"
    assert (tmp_path / "state" / "tokens.json").stat().st_mode & 0o777 == 0o600


def test_client_metadata_uses_the_loopback_callback():
    metadata = mcp_client.client_metadata()
    assert [str(uri) for uri in metadata.redirect_uris] == ["http://127.0.0.1:8765/callback"]
    assert metadata.token_endpoint_auth_method == "none"
    assert set(metadata.grant_types) == {"authorization_code", "refresh_token"}


def test_mcp_client_prints_its_usage_through_the_run_helper(tmp_path):
    run = runs.Run(tmp_path / "r1", "guide-1")
    run.env_file.parent.mkdir(parents=True)
    run.env_file.write_text("")
    runs.write_helpers(run, cases.CASES["guide-1"])
    result = subprocess.run(
        [str(run.base / "run"), "mcp-client"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 2, result.stdout
    assert "mcp-client login <url>" in result.stdout


def test_browser_parses_steps():
    steps = ["goto:https://x/a?b=c", "fill:password=p=q", "click:Sign in", "show"]
    assert browser.parse(steps) == [
        ("goto", "https://x/a?b=c"),
        ("fill", "password=p=q"),
        ("click", "Sign in"),
        ("show", ""),
    ]
    with pytest.raises(ValueError):
        browser.parse(["type:x"])


class FormPage(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            b'<form method="post" action="/done"><input name="password">'
            b"<button>Sign in</button></form>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        sent = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>received " + sent + b"</p>")

    def log_message(self, *args):
        pass


@pytest.mark.browser
def test_browser_fills_and_submits_a_local_form(capsys):
    server = HTTPServer(("127.0.0.1", 0), FormPage)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        steps = [f"goto:http://127.0.0.1:{port}/", "fill:password=s3cret", "click:Sign in"]
        assert browser.main(steps) == 0
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert f"URL: http://127.0.0.1:{port}/done" in out
    assert "received password=s3cret" in out
