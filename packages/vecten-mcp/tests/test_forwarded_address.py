"""Per-address limits count the client NGINX saw, through Uvicorn's own proxy handling."""

import re
import warnings

import pytest
import uvicorn
from starlette.applications import Starlette

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
    )
    from starlette.testclient import TestClient

from vecten_mcp import __main__ as entry
from vecten_mcp.auth.identity import PasswordBackend
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.routes import auth_routes
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.state import StateStore

NGINX = ("172.18.0.3", 40000)
PASSWORD = "long-fixture-password"


class Secrets:
    def __init__(self):
        self.values = {}

    def list_secrets(self):
        return list(self.values)

    def get_secret(self, name):
        return self.values[name]

    def put_secret(self, name, value):
        self.values[name] = value


@pytest.fixture
def served(monkeypatch):
    """The auth routes wrapped exactly as `python -m vecten_mcp` has Uvicorn wrap the app."""
    calls = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    entry.main()
    (options,) = calls
    state = StateStore("sqlite://")
    person = state.invite("jane@example.com", "Jane", "jane", provisioned=True)
    backend = PasswordBackend(state)
    service = OAuthService(
        state, SigningKeys(Secrets(), "https://mcp.example"), "https://mcp.example", backend
    )
    config = uvicorn.Config(
        Starlette(routes=auth_routes(service)),
        proxy_headers=options.get("proxy_headers", True),
        forwarded_allow_ips=options.get("forwarded_allow_ips"),
    )
    config.load()

    def client(peer):
        return TestClient(config.loaded_app, base_url="https://mcp.example", client=peer)

    return client, backend, person


def set_password(http, token, forwarded=None):
    page = http.get("/set-password")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    return http.post(
        "/set-password",
        data={"csrf": csrf, "token": token, "password": PASSWORD},
        headers=headers,
    )


def exhaust(http, forwarded):
    for _ in range(30):
        assert set_password(http, "not-a-token", forwarded).status_code == 400
    assert set_password(http, "not-a-token", forwarded).status_code == 429


def test_one_client_exhausting_its_limit_leaves_another_client_through_nginx(served):
    client, backend, person = served
    with client(NGINX) as http:
        exhaust(http, "203.0.113.7")
        response = set_password(http, backend.setup_link(person["email"]), "198.51.100.4")
    assert response.status_code == 200


def test_the_address_nginx_appended_counts_not_one_the_client_wrote(served):
    client, backend, person = served
    with client(NGINX) as http:
        for n in range(30):
            set_password(http, "not-a-token", f"192.0.2.{n}, 203.0.113.7")
        assert set_password(http, "not-a-token", "192.0.2.99, 203.0.113.7").status_code == 429
        assert (
            set_password(http, backend.setup_link(person["email"]), "198.51.100.4").status_code
            == 200
        )


def test_a_public_peer_cannot_name_its_own_address(served):
    client, backend, person = served
    with client(("203.0.113.50", 40000)) as http:
        for n in range(30):
            set_password(http, "not-a-token", f"192.0.2.{n}")
        assert set_password(http, "not-a-token", "192.0.2.200").status_code == 429
    with client(NGINX) as http:
        assert (
            set_password(http, backend.setup_link(person["email"]), "198.51.100.4").status_code
            == 200
        )
