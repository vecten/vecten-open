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


def test_verified_sign_in_hook_owns_only_its_declared_human_memberships():
    from vecten_mcp.access import AccessService
    from vecten_mcp.auth.service import OAuthService

    class Roles:
        def create_group(self, role):
            pass

        def membership(self, role, name, grant):
            pass

        def drop_group(self, role):
            pass

    seen = []

    def handler(*, identity, verified_claims, set_membership):
        seen.append((identity.email, verified_claims["sub"]))
        with pytest.raises(ValueError, match="does not manage"):
            set_membership("manual", True)
        set_membership("not_created", True)
        set_membership("finance", True)

    backend = oidc_fixture({})
    state = backend.state
    access = AccessService(
        state,
        Roles(),
        sign_in_hooks={"example": {"groups": ["finance", "not_created"], "handler": handler}},
    )
    service = OAuthService(
        state, None, "https://mcp.example", backend, on_sign_in=access.on_sign_in
    )
    person = state.invite("jane@example.com", "Jane", "jane", provisioned=True)
    access.create_group("finance")
    access.create_group("manual")
    access.membership("manual", person["id"])
    started = backend.begin("browser")
    backend.last_nonce = started["nonce"]
    identity, _ = backend.callback(started["state"], "code")
    service.login_identity(identity)
    assert seen == [(person["email"], identity.subject)]
    assert {r["group"]: r["source"] for r in access.health()["membership_sources"]} == {
        "finance": "example",
        "default": "local",
        "manual": "local",
    }
    with pytest.raises(ValueError, match="example manages"):
        access.membership("finance", person["id"], False)
    assert access.delete_group("finance") == {"deleted": "finance"}
    assert state.groups_for(person["id"]) == {"default", "manual"}


@pytest.mark.parametrize("first_admin", [True, False])
def test_unprovisioned_sign_in_skips_membership_hooks(first_admin):
    from vecten_mcp.auth.service import OAuthService

    backend = oidc_fixture({})
    email = "jane@example.com"
    if not first_admin:
        backend.state.invite(email, "Jane", "jane")

    def unexpected_hook(identity, principal):
        pytest.fail("membership hook ran before provisioning")

    oauth = OAuthService(
        backend.state,
        None,
        "https://mcp.example",
        backend,
        admins=[email] if first_admin else [],
        on_sign_in=unexpected_hook,
    )
    started = backend.begin("browser")
    backend.last_nonce = started["nonce"]
    identity, _ = backend.callback(started["state"], "code")
    principal = oauth.login_identity(identity)
    assert not principal["provisioned"]
    assert principal["admin"] == first_admin


@pytest.mark.parametrize(
    "declarations",
    [
        {"a": ["team"], "b": ["team"]},
        {"a": ["default"]},
        {"local": []},
        {"a": ["bad name!"]},
    ],
)
def test_sign_in_hook_loader_rejects_ambiguous_or_invalid_owners(declarations, monkeypatch):
    from types import SimpleNamespace

    from vecten_mcp import extensions
    from vecten_shared.output import Failure

    def factory(names):
        return lambda *, config: {"groups": names, "handler": lambda **kwargs: None}

    hooks = {name: factory(groups) for name, groups in declarations.items()}
    monkeypatch.setattr(
        extensions,
        "load_group",
        lambda config, group: hooks if group == "vecten.mcp.sign_in" else {},
    )
    monkeypatch.setattr(extensions, "selected_entry_points", lambda *args: [])
    with pytest.raises(Failure, match="sign-in extension"):
        extensions.McpExtensions.from_config(SimpleNamespace(mcp=SimpleNamespace(auth="password")))
