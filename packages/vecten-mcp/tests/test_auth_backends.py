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


def test_signed_group_claim_changes_membership_on_each_login():
    from vecten_mcp.access import AccessService
    from vecten_mcp.auth.service import OAuthService
    from vecten_shared.config import OidcGroups

    class Roles:
        def __init__(self):
            self.changes = []

        def create_group(self, role):
            pass

        def membership(self, role, name, grant):
            self.changes.append((role, name, grant))

    claims = {"business_units": ["department-73", "unknown"]}
    backend = oidc_fixture(claims)
    groups = OidcGroups(
        claim="business_units",
        scope="groups",
        bindings={"finance": "department-73", "uncreated": "unknown"},
    )
    backend.groups = groups
    roles = Roles()
    access = AccessService(backend.state, roles, oidc_groups=groups)
    service = OAuthService(
        backend.state, None, "https://mcp.example", backend, sync_groups=access.sync_oidc_groups
    )
    person = backend.state.invite("jane@example.com", "Jane", "jane", provisioned=True)
    access.create_group("finance")
    access.create_group("manual")
    access.membership("manual", person["id"])

    def login():
        started = backend.begin("browser")
        assert "scope=openid+email+profile+groups" in started["url"]
        backend.last_nonce = started["nonce"]
        identity, flow = backend.callback(started["state"], "code")
        assert flow == "browser"
        service.login_identity(identity)

    login()
    login()
    assert roles.changes.count(("finance", "jane", True)) == 1
    origins = {r["group"]: r["source"] for r in access.health()["membership_sources"]}
    assert origins == {"default": "local", "manual": "local", "finance": "oidc"}
    with pytest.raises(ValueError, match="identity provider"):
        access.membership("finance", person["id"], False)
    claims["business_units"] = []
    login()
    assert backend.state.groups_for(person["id"]) == {"default", "manual"}
    assert roles.changes[-1] == ("finance", "jane", False)


@pytest.mark.parametrize("value", [None, "finance", [3]])
def test_groups_require_complete_signed_string_list(value):
    from vecten_shared.config import OidcGroups

    backend = oidc_fixture({} if value is None else {"teams": value})
    backend.groups = OidcGroups(claim="teams", bindings={"finance": "value"})
    started = backend.begin("flow")
    backend.last_nonce = started["nonce"]
    with pytest.raises(ValueError, match="group claim"):
        backend.callback(started["state"], "code")
