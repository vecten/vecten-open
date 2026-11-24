"""Run against the pinned Airflow distribution in the image test tier."""

import hashlib
import time
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

pytest.importorskip("airflow.api_fastapi")

from airflow.api_fastapi.auth.managers.base_auth_manager import COOKIE_NAME_JWT_TOKEN
from airflow.api_fastapi.auth.managers.simple.services import login as password_login
from airflow.api_fastapi.auth.managers.simple.user import SimpleAuthManagerUser
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from vecten_platform.airflow_oidc import OidcAuthManager

ISSUER = "https://identity.example"
BASE = "https://airflow.example"


@pytest.fixture
def flow(monkeypatch, tmp_path):
    (tmp_path / "vecten.toml").write_text(
        'users = 3\n[project]\nname = "acme"\nscaffold_version = "0.1.1"\n'
        f'[mcp]\nauth = "oidc"\noidc_issuer = "{ISSUER}"\noidc_client_id = "client"\n'
        'admins = ["owner@example.org"]\n[airflow]\nauth = "oidc"\n'
    )
    monkeypatch.setenv("VECTEN_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AIRFLOW__API__BASE_URL", BASE)
    monkeypatch.setenv("AIRFLOW__API__SECRET_KEY", "test-cookie-signing-key")
    monkeypatch.setenv("AIRFLOW__API_AUTH__JWT_SECRET", "test-airflow-signing-key-" * 4)
    key = RSAKey.generate_key(2048, parameters={"kid": "mock-issuer"})
    public = key.as_dict(private=False)
    provider = {"claims": {}, "signed_by": key, "metadata": {}}

    def respond(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/authorize",
                    "token_endpoint": ISSUER + "/token",
                    "jwks_uri": ISSUER + "/keys",
                    **provider["metadata"],
                },
            )
        if request.url.path == "/keys":
            return httpx2.Response(200, json={"keys": [public]})
        assert request.url.path == "/token"
        fields = parse_qs(request.content.decode())
        verifier = fields["code_verifier"][0].encode()
        assert (
            urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode()
            == provider["challenge"]
        )
        assert fields["redirect_uri"] == [BASE + "/auth/oidc/callback"]
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": "subject",
            "email": "Owner@Example.org",
            "email_verified": True,
            "aud": "client",
            "nonce": provider["nonce"],
            "iat": now,
            "exp": now + 60,
            **provider["claims"],
        }
        signed = jwt.encode({"alg": "RS256", "kid": "mock-issuer"}, claims, provider["signed_by"])
        return httpx2.Response(
            200,
            json={
                "access_token": "unused",
                "token_type": "Bearer",
                "id_token": provider.get("id_token", signed),
            },
        )

    register = OAuth.register

    def with_provider(self, **kwargs):
        kwargs["client_kwargs"]["transport"] = httpx2.MockTransport(respond)
        return register(self, **kwargs)

    monkeypatch.setattr(OAuth, "register", with_provider)
    manager = OidcAuthManager()
    app = FastAPI()
    app.mount("/auth", manager.get_fastapi_app())
    return TestClient(app, base_url=BASE), provider, manager


def start(browser, provider):
    login = browser.get("/auth/login?next=https://outside.example", follow_redirects=False)
    assert login.status_code == 302
    redirect = urlsplit(login.headers["location"])
    assert redirect.netloc == "identity.example"
    parameters = parse_qs(redirect.query)
    assert parameters["code_challenge_method"] == ["S256"]
    provider["challenge"] = parameters["code_challenge"][0]
    provider["nonce"] = parameters["nonce"][0]
    return {"code": "issued-code", "state": parameters["state"][0]}


@pytest.mark.parametrize(
    "claims,status",
    [
        ({}, 303),
        ({"email": "other@example.org"}, 403),
        ({"email_verified": False}, 403),
        ({"nonce": "incorrect"}, 401),
        ({"iss": "https://other.example"}, 401),
        ({"aud": "other-client"}, 401),
        ({"exp": 1}, 401),
    ],
)
def test_browser_sign_in(flow, claims, status):
    browser, provider, manager = flow
    provider["claims"] = claims
    response = browser.get(
        "/auth/oidc/callback", params=start(browser, provider), follow_redirects=False
    )
    assert response.status_code == status
    assert (COOKIE_NAME_JWT_TOKEN in response.cookies) == (status == 303)
    if status == 303:
        import jwt as pyjwt

        claims = pyjwt.decode(
            response.cookies[COOKIE_NAME_JWT_TOKEN], options={"verify_signature": False}
        )
        user = manager.deserialize_user(claims)
        assert (user.username, user.role) == ("owner@example.org", "admin")
        assert response.headers["location"] == "/"
        cookie = next(
            line
            for line in response.headers.get_list("set-cookie")
            if line.startswith(COOKIE_NAME_JWT_TOKEN + "=")
        )
        assert "Secure" in cookie and "HttpOnly" in cookie


def test_unsigned_identity_or_unknown_state_cannot_create_session(flow):
    browser, provider, _ = flow
    assert browser.get("/auth/oidc/callback?code=x&state=unknown").status_code == 401
    provider["signed_by"] = RSAKey.generate_key(2048)
    params = start(browser, provider)
    assert browser.get("/auth/oidc/callback", params=params).status_code == 401
    assert COOKIE_NAME_JWT_TOKEN not in browser.cookies


def test_provider_must_identify_the_configured_issuer(flow):
    browser, provider, _ = flow
    provider["metadata"] = {"issuer": "https://other.example"}
    assert browser.get("/auth/login").status_code == 503


def test_provider_offering_only_symmetric_signatures_is_unavailable(flow):
    browser, provider, _ = flow
    provider["metadata"]["id_token_signing_alg_values_supported"] = ["HS256"]
    assert browser.get("/auth/login").status_code == 503
    assert COOKIE_NAME_JWT_TOKEN not in browser.cookies


def test_symmetric_id_token_is_refused_even_with_a_valid_session(flow):
    import jwt as pyjwt

    browser, provider, _ = flow
    provider["id_token"] = pyjwt.encode(
        {"sub": "test-subject"}, "fixture-client-secret-" * 3, algorithm="HS256"
    )
    response = browser.get("/auth/oidc/callback", params=start(browser, provider))
    assert response.status_code == 401
    assert COOKIE_NAME_JWT_TOKEN not in browser.cookies


def test_reloaded_admin_list_applies_to_existing_browser_tokens(flow):
    import jwt as pyjwt
    from fastapi import HTTPException

    browser, provider, manager = flow
    response = browser.get(
        "/auth/oidc/callback", params=start(browser, provider), follow_redirects=False
    )
    claims = pyjwt.decode(
        response.cookies[COOKIE_NAME_JWT_TOKEN], options={"verify_signature": False}
    )
    manager.admins = {"replacement@example.org"}
    with pytest.raises(HTTPException) as denied:
        manager.deserialize_user(claims)
    assert denied.value.status_code == 403


def test_password_tokens_are_read_only_and_require_credentials(flow, monkeypatch):
    browser, _, manager = flow
    monkeypatch.setattr(password_login, "get_auth_manager", lambda: manager)
    monkeypatch.setattr(
        password_login.SimpleAuthManager,
        "get_users",
        lambda: [SimpleAuthManagerUser(username="reader", role="admin")],
    )
    monkeypatch.setattr(
        password_login.SimpleAuthManager, "get_passwords", lambda: {"reader": "test-password"}
    )
    response = browser.post("/auth/token", json={"username": "reader", "password": "test-password"})
    assert response.status_code == 201
    import jwt as pyjwt

    claims = pyjwt.decode(response.json()["access_token"], options={"verify_signature": False})
    assert "vecten_oidc" not in claims
    assert manager.deserialize_user(claims).role == "viewer"
    assert manager.deserialize_user({"sub": "legacy", "role": "admin"}).role == "viewer"
    assert (
        browser.post("/auth/token", json={"username": "reader", "password": "wrong"}).status_code
        == 401
    )
    assert browser.get("/auth/token/login").status_code == 404
