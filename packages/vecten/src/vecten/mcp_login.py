"""Browser authorization with a single-use loopback redirect and PKCE."""

import base64
import hashlib
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from vecten.mcp_client import safe_request
from vecten_shared.output import ExitCode, Failure


def loopback_login(issuer, http, cache, *, open_browser=webbrowser.open):
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = parsed.path == "/callback" and secrets.compare_digest(
                query.get("state", [""])[0], state
            )
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Return to the Vecten CLI." if valid else b"Invalid callback.")
            if valid:
                result.update(
                    code=query.get("code", [None])[0], error=query.get("error", [None])[0]
                )

        def log_message(self, *_):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        response, client = safe_request(
            http,
            "POST",
            issuer + "/register",
            json={"client_name": "Vecten CLI", "redirect_uris": [redirect]},
        )
        if response.status_code != 201:
            raise Failure(ExitCode.REFUSED, "CLI registration failed")
        query = dict(
            response_type="code",
            client_id=client["client_id"],
            redirect_uri=redirect,
            code_challenge=challenge,
            code_challenge_method="S256",
            resource=issuer + "/mcp",
            state=state,
        )
        if not open_browser(issuer + "/authorize?" + urlencode(query)):
            raise Failure(
                ExitCode.PREREQUISITE, "no browser available; use vecten login without --browser"
            )
        deadline = time.monotonic() + 180
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code"):
        raise Failure(ExitCode.REFUSED, "browser authorization was declined or timed out")
    response, tokens = safe_request(
        http,
        "POST",
        issuer + "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client["client_id"],
            "code": result["code"],
            "code_verifier": verifier,
            "redirect_uri": redirect,
            "resource": issuer + "/mcp",
        },
    )
    if response.status_code != 200:
        raise Failure(ExitCode.REFUSED, "authorization code exchange failed")
    cache.save(
        issuer,
        {
            **tokens,
            "client_id": client["client_id"],
            "expires_at": time.time() + tokens["expires_in"],
        },
    )
    return {"authenticated": True, "issuer": issuer}
