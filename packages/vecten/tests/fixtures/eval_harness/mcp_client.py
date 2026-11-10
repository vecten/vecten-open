"""mcp-client: the person's MCP client, like a chat app's connector; it keeps its own sign-in.

    mcp-client login <url>               register, print the sign-in page, wait for the browser
    mcp-client tools <url>               list the tools
    mcp-client call <url> <tool> <json>  call one tool, print its result with its _meta

A subagent cannot add an MCP server to itself, so in an evaluation this stands for the
assistant a person connects. Its registration and tokens live under MCP_CLIENT_STATE, one per
run; the agent driving it never sees a password or a token.
"""

import asyncio
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp.client import Client
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
SIGN_IN_TIMEOUT = 600


class FileStorage(TokenStorage):
    def __init__(self, state: Path):
        self.state = Path(state)

    def _read(self, name, model):
        path = self.state / name
        return model.model_validate_json(path.read_text()) if path.exists() else None

    def _write(self, name, value):
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / name
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(value.model_dump_json())

    async def get_tokens(self):
        return self._read("tokens.json", OAuthToken)

    async def set_tokens(self, tokens):
        self._write("tokens.json", tokens)

    async def get_client_info(self):
        return self._read("client.json", OAuthClientInformationFull)

    async def set_client_info(self, info):
        self._write("client.json", info)


def client_metadata() -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="The person's assistant",
        redirect_uris=[REDIRECT],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def wait_for_callback() -> dict[str, str]:
    got: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            got.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    server.timeout = SIGN_IN_TIMEOUT
    server.handle_request()
    server.server_close()
    return got


async def redirect(url):
    print(f"Open this page in the browser to sign in: {url}", flush=True)


async def callback():
    got = await asyncio.to_thread(wait_for_callback)
    if "code" not in got:
        raise SystemExit(f"sign-in did not complete: {got}")
    return AuthorizationCodeResult(code=got["code"], state=got.get("state"), iss=got.get("iss"))


def show(result):
    data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    print(json.dumps(data, indent=2))


async def session(command: str, url: str, rest: list[str], state: Path) -> None:
    provider = OAuthClientProvider(url, client_metadata(), FileStorage(state), redirect, callback)
    verify = os.environ.get("SSL_CERT_FILE", True)
    async with httpx2.AsyncClient(auth=provider, verify=verify, timeout=120) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            if command == "login":
                print("signed in", flush=True)
            elif command == "tools":
                for tool in (await client.list_tools()).tools:
                    print(f"{tool.name}: {(tool.description or '').splitlines()[0][:160]}")
                    print(f"  input: {json.dumps(tool.input_schema)}")
            else:
                arguments = json.loads(rest[1]) if len(rest) > 1 else {}
                show(await client.call_tool(rest[0], arguments))


def main(argv: list[str]) -> int:
    commands = {"login": 0, "tools": 0, "call": 1}
    if len(argv) < 2 or argv[0] not in commands or len(argv) - 2 < commands[argv[0]]:
        print(__doc__)
        return 2
    state = os.environ.get("MCP_CLIENT_STATE")
    if not state:
        print("MCP_CLIENT_STATE names no folder; run mcp-client through the run wrapper")
        return 2
    asyncio.run(session(argv[0], argv[1], argv[2:], Path(state)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
