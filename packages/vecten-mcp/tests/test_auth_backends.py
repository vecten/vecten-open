import httpx
import pytest

from vecten_mcp.auth.identity import OIDCBackend, PasswordBackend
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.limits import RateLimit, RateLimiter
from vecten_mcp.state import StateStore


class Secrets:
    def __init__(self):
        self.values = {}

    def list_secrets(self):
        return list(self.values)

    def get_secret(self, name):
        return self.values[name]

    def put_secret(self, name, value):
        self.values[name] = value


def test_password_one_time_link_hash_and_throttles():
    store = StateStore("sqlite://")
    store.invite("jane@example.com", "Jane", "jane")
    now = [1]
    backend = PasswordBackend(store, clock=lambda: now[0])
    token = backend.setup_link("jane@example.com")
    assert token not in str(store.scan("password_links"))
    backend.set_password(token, "long-fixture-password")
    assert store.get("passwords", "jane@example.com")["hash"].startswith("$argon2id$")
    with pytest.raises(ValueError):
        backend.set_password(token, "long-fixture-password")
    assert (
        backend.authenticate("Jane@example.com", "long-fixture-password", "ip").email
        == "jane@example.com"
    )
    for _ in range(5):
        with pytest.raises(ValueError):
            backend.authenticate("jane@example.com", "wrong", "ip")
    with pytest.raises(RateLimit):
        backend.authenticate("jane@example.com", "long-fixture-password", "ip2")
    now[0] += 301
    assert backend.authenticate("jane@example.com", "long-fixture-password", "ip").name == "Jane"


def test_password_link_expiry_and_unknown_account():
    store = StateStore("sqlite://")
    backend = PasswordBackend(store, clock=lambda: 10)
    with pytest.raises(ValueError):
        backend.setup_link("absent@example.com")
    with pytest.raises(ValueError):
        backend.authenticate("absent@example.com", "wrong", "ip")


def test_limiter_applies_address_limit_across_identities():
    limiter = RateLimiter(limit=2, window=30, clock=lambda: 1)
    limiter.hit("ip")
    limiter.hit("ip")
    with pytest.raises(RateLimit):
        limiter.hit("ip")


def oidc_fixture(claims=None):
    secrets = Secrets()
    signer = SigningKeys(secrets, issuer="https://idp.example", clock=lambda: 100)
    state = StateStore("sqlite://")

    def handle(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json=dict(
                    issuer="https://idp.example",
                    authorization_endpoint="https://idp.example/authorize",
                    token_endpoint="https://idp.example/token",
                    jwks_uri="https://idp.example/jwks",
                ),
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=signer.jwks())
        if request.url.path == "/token":
            assert b"code_verifier=" in request.content
            token = signer.sign(
                "subject",
                "client",
                200,
                extra={
                    "email": "jane@example.com",
                    "email_verified": True,
                    "name": "Jane",
                    "nonce": backend.last_nonce,
                    **(claims or {}),
                },
            )
            return httpx.Response(200, json={"id_token": token})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    backend = OIDCBackend(
        state,
        "https://idp.example",
        "client",
        "https://mcp.example/oidc/callback",
        client=client,
        clock=lambda: 100,
    )
    return backend


def test_oidc_validates_identity_state_and_single_use():
    backend = oidc_fixture()
    result = backend.begin("flow")
    backend.last_nonce = result["nonce"]
    assert "code_challenge_method=S256" in result["url"]
    identity, flow = backend.callback(result["state"], "code")
    assert identity.subject == "subject" and flow == "flow"
    with pytest.raises(ValueError):
        backend.callback(result["state"], "code")


@pytest.mark.parametrize(
    "claims",
    [{"email_verified": False}, {"nonce": "bad"}, {"aud": "different"}, {"iss": "https://other"}],
)
def test_oidc_rejects_unverified_or_misbound_id_token(claims):
    backend = oidc_fixture(claims)
    result = backend.begin("flow")
    backend.last_nonce = result["nonce"]
    with pytest.raises(ValueError):
        backend.callback(result["state"], "code")


def test_unconfigured_oidc_refuses_login():
    backend = OIDCBackend(StateStore("sqlite://"), None, None, "https://mcp.example/callback")
    with pytest.raises(ValueError, match="OIDC"):
        backend.begin("flow")


def test_password_link_cannot_cross_deleted_or_disabled_principal():
    from sqlalchemy import delete

    from vecten_mcp.state import principals

    state = StateStore("sqlite://")
    old = state.invite("owner@example.com", "Owner", "owner")
    backend = PasswordBackend(state)
    link = backend.setup_link(old["email"])
    with state.transaction() as conn:
        conn.execute(delete(principals).where(principals.c.id == old["id"]))
    replacement = state.invite(old["email"], "Replacement", "replacement")
    with pytest.raises(ValueError):
        backend.set_password(link, "long-enough-password")
    link = backend.setup_link(replacement["email"])
    state.update_principal(replacement["id"], enabled=False)
    with pytest.raises(ValueError):
        backend.set_password(link, "long-enough-password")
