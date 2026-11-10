import re
import warnings
from html import unescape
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore", message="The anyio.abc.BlockingPortal alias", category=DeprecationWarning
    )
    from starlette.testclient import TestClient

from vecten_mcp.auth.identity import PasswordBackend, challenge, digest
from vecten_mcp.auth.keys import SigningKeys
from vecten_mcp.auth.routes import auth_routes
from vecten_mcp.auth.service import OAuthService
from vecten_mcp.identity import Identity
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


@pytest.fixture
def auth():
    now = [100]
    state = StateStore("sqlite://")
    p = state.invite("jane@example.com", "Jane", "jane", provisioned=True)
    keys = SigningKeys(Secrets(), "https://mcp.example", clock=lambda: now[0])
    backend = PasswordBackend(state, clock=lambda: now[0])
    backend.set_password(backend.setup_link(p["email"]), "long-fixture-password")
    service = OAuthService(
        state,
        keys,
        "https://mcp.example",
        backend,
        admins=["jane@example.com"],
        clock=lambda: now[0],
    )
    return service, now, p


def issue(auth):
    service, _, p = auth
    client = service.register(
        {"client_name": "Example", "redirect_uris": ["http://127.0.0.1:9000/callback"]}, "ip"
    )
    flow = service.authorize(
        client["client_id"],
        client["redirect_uris"][0],
        challenge("a" * 43),
        "S256",
        service.resource,
        "state",
    )
    service.authenticate(flow, Identity("password", p["email"], p["email"], p["name"]))
    redirect = service.consent(flow, True)
    code = parse_qs(urlparse(redirect).query)["code"][0]
    token = service.exchange(
        code, client["client_id"], client["redirect_uris"][0], "a" * 43, service.resource
    )
    return client, token


def test_code_pkce_consent_and_hashed_token_state(auth):
    service, _, p = auth
    client, tokens = issue(auth)
    resolved = service.resolve(tokens["access_token"])
    assert resolved.principal["id"] == p["id"] and resolved.principal["admin"]
    assert resolved.groups == {"default"}
    for kind in ("access", "refresh", "codes", "clients"):
        assert tokens["access_token"] not in str(service.state.scan(kind))
        assert tokens["refresh_token"] not in str(service.state.scan(kind))
    with pytest.raises(ValueError):
        service.resolve(tokens["access_token"], audience="https://other/mcp")
    with pytest.raises(ValueError):
        service.authorize(
            client["client_id"],
            "http://127.0.0.1:9000/other",
            challenge("a" * 43),
            "S256",
            service.resource,
            "",
        )
    with pytest.raises(ValueError):
        service.authorize(
            client["client_id"], client["redirect_uris"][0], "plain", "plain", service.resource, ""
        )


def test_refresh_rotation_replay_revokes_session_and_max_lifetime(auth):
    service, now, _ = auth
    client, first = issue(auth)
    second = service.refresh(first["refresh_token"], client["client_id"])
    assert service.resolve(second["access_token"])
    # OAuth clients start a new authorization only on invalid_grant.
    with pytest.raises(ValueError, match="^invalid_grant$"):
        service.refresh(first["refresh_token"], client["client_id"])
    with pytest.raises(ValueError):
        service.resolve(second["access_token"])
    client, token = issue(auth)
    now[0] += 1104801
    with pytest.raises(ValueError, match="^invalid_grant$"):
        service.refresh(token["refresh_token"], client["client_id"])


def test_revocation_and_disable_are_immediate(auth):
    service, _, p = auth
    client, token = issue(auth)
    service.revoke_client(p["id"], client["client_id"])
    with pytest.raises(ValueError):
        service.resolve(token["access_token"])
    client, token = issue(auth)
    service.state.update_principal(p["id"], enabled=False)
    with pytest.raises(ValueError):
        service.resolve(token["access_token"])
    with pytest.raises(ValueError, match="^invalid_grant$"):
        service.refresh(token["refresh_token"], client["client_id"])


def test_service_credentials_are_hashed_and_humans_refused(auth):
    service, _, p = auth
    with pytest.raises(ValueError, match="human"):
        service.service_credentials(p["id"])
    svc = service.state.create_principal(
        Identity("service", "reporting", "reporting@service.invalid", "Reporting"),
        "svc_reporting",
        kind="service",
        provisioned=True,
    )
    credentials = service.service_credentials(svc["id"])
    assert credentials["client_secret"] not in str(service.state.scan("clients"))
    token = service.client_credentials(
        credentials["client_id"], credentials["client_secret"], service.resource
    )
    assert service.resolve(token["access_token"]).principal["kind"] == "service"
    with pytest.raises(ValueError):
        service.client_credentials(credentials["client_id"], "wrong", service.resource)


def test_device_polling_consent_and_expiry(auth):
    service, now, p = auth
    client = service.register({"client_name": "CLI", "redirect_uris": []}, "ip")
    device = service.device(client["client_id"], service.resource)
    with pytest.raises(ValueError, match="authorization_pending"):
        service.device_token(device["device_code"], client["client_id"])
    with pytest.raises(ValueError, match="slow_down"):
        service.device_token(device["device_code"], client["client_id"])
    service.approve_device(
        device["user_code"], Identity("password", p["email"], p["email"], p["name"]), True
    )
    now[0] += 11
    tokens = service.device_token(device["device_code"], client["client_id"])
    assert service.resolve(tokens["access_token"]).principal["id"] == p["id"]
    with pytest.raises(ValueError):
        service.device_token(device["device_code"], client["client_id"])


def test_http_flow_requires_cookie_csrf_and_consent(auth):
    service, _, p = auth
    with TestClient(
        Starlette(routes=auth_routes(service)), base_url="https://mcp.example"
    ) as client:
        registration = client.post(
            "/register",
            json={
                "client_name": "<script>bad</script>",
                "redirect_uris": ["http://127.0.0.1:9000/callback"],
            },
        ).json()
        page = client.get(
            "/authorize",
            params=dict(
                client_id=registration["client_id"],
                redirect_uri=registration["redirect_uris"][0],
                code_challenge=challenge("a" * 43),
                code_challenge_method="S256",
                response_type="code",
                resource=service.resource,
                state="state",
            ),
        )
        assert page.status_code == 200
        flow = re.search(r'name="flow" value="([^"]+)"', page.text)[1]
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        bad = client.post(
            "/login",
            data=dict(flow=flow, csrf="wrong", email=p["email"], password="long-fixture-password"),
        )
        assert bad.status_code == 400
        consent = client.post(
            "/login",
            data=dict(flow=flow, csrf=csrf, email=p["email"], password="long-fixture-password"),
        )
        assert "Approve" in consent.text and "<script>bad</script>" not in consent.text
        redir = client.post(
            "/consent", data=dict(flow=flow, csrf=csrf, decision="approve"), follow_redirects=False
        )
        assert redir.status_code == 200
        target = unescape(re.search(r'<a href="([^"]+)"', redir.text)[1])
        code = parse_qs(urlparse(target).query)["code"][0]
        payload = dict(
            grant_type="authorization_code",
            code=code,
            client_id=registration["client_id"],
            redirect_uri=registration["redirect_uris"][0],
            code_verifier="a" * 43,
            resource=service.resource,
        )
        assert client.post("/token", data=payload).status_code == 200
        assert client.post("/token", data=payload).status_code == 400
        assert (
            client.get("/.well-known/oauth-authorization-server").json()["issuer"] == service.issuer
        )


def _code_for(service, p, registration):
    flow = service.authorize(
        registration["client_id"],
        registration["redirect_uris"][0],
        challenge("a" * 43),
        "S256",
        service.resource,
        "state",
    )
    service.authenticate(flow, Identity("password", p["email"], p["email"], p["name"]))
    return parse_qs(urlparse(service.consent(flow, True)).query)["code"][0]


@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
def test_confidential_registration_gets_a_secret_the_token_endpoint_checks(auth, method):
    # Claude registers as a confidential client, as the metadata's auth methods allow.
    service, _, p = auth
    with TestClient(Starlette(routes=auth_routes(service)), base_url="https://mcp.example") as http:
        response = http.post(
            "/register",
            json={
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": method,
            },
        )
        assert response.status_code == 201, response.text
        registration = response.json()
        secret = registration["client_secret"]
        assert registration["token_endpoint_auth_method"] == method
        assert registration["client_secret_expires_at"] == 0
        assert isinstance(registration["client_id_issued_at"], int)
        assert "client_credentials" not in registration["grant_types"]
        assert not {"used", "created_at", "secret_hash"} & set(registration)
        assert secret not in str(service.state.scan("clients"))

        def token(data, secret=None):
            data = dict(data, client_id=registration["client_id"])
            if secret is None:
                return http.post("/token", data=data)
            if method == "client_secret_post":
                return http.post("/token", data=dict(data, client_secret=secret))
            return http.post("/token", data=data, auth=(registration["client_id"], secret))

        grant = dict(
            grant_type="authorization_code",
            redirect_uri=registration["redirect_uris"][0],
            code_verifier="a" * 43,
            resource=service.resource,
        )
        for wrong in (None, "wrong-secret"):
            refused = token(dict(grant, code=_code_for(service, p, registration)), wrong)
            assert refused.json()["error"] == "invalid_client", refused.text
        issued = token(dict(grant, code=_code_for(service, p, registration)), secret)
        assert issued.status_code == 200, issued.text
        refresh = dict(grant_type="refresh_token", refresh_token=issued.json()["refresh_token"])
        assert token(refresh).json()["error"] == "invalid_client"
        assert token(refresh, secret).status_code == 200
        services_only = token(
            dict(grant_type="client_credentials", resource=service.resource), secret
        )
        assert services_only.status_code == 400 and "access_token" not in services_only.json()


def test_registration_refusals_name_the_metadata_at_fault(auth):
    service, _, _ = auth
    with TestClient(Starlette(routes=auth_routes(service)), base_url="https://mcp.example") as http:
        method = http.post(
            "/register",
            json={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "token_endpoint_auth_method": "private_key_jwt",
            },
        )
        assert method.status_code == 400
        assert method.json()["error"] == "invalid_client_metadata"
        assert "private_key_jwt" in method.json()["error_description"]
        redirect = http.post("/register", json={"redirect_uris": ["http://claude.ai/callback"]})
        assert redirect.status_code == 400
        assert redirect.json()["error"] == "invalid_redirect_uri"
        assert redirect.json()["error_description"]


def test_denied_consent_issues_no_code_and_expired_access_fails(auth):
    service, now, _ = auth
    client, token = issue(auth)
    now[0] += 3601
    with pytest.raises(ValueError):
        service.resolve(token["access_token"])
    p = service.state.principals()[0]
    flow = service.authorize(
        client["client_id"],
        client["redirect_uris"][0],
        challenge("b" * 43),
        "S256",
        service.resource,
        "s",
    )
    service.authenticate(flow, Identity("password", p["email"], p["email"], p["name"]))
    result = parse_qs(urlparse(service.consent(flow, False)).query)
    assert result["error"] == ["access_denied"] and "code" not in result


def test_registration_throttle_and_prune_only_unused_clients(auth):
    service, now, _ = auth
    client, _ = issue(auth)
    for _ in range(9):
        service.register({"redirect_uris": []}, "ip")
    with pytest.raises(ValueError, match="rate limit"):
        service.register({"redirect_uris": []}, "ip")
    now[0] += 86401
    service.maintain()
    assert [cid for cid, _ in service.state.scan("clients")] == [client["client_id"]]


def test_device_polling_does_not_extend_expiry(auth):
    service, now, _ = auth
    client = service.register({"redirect_uris": []}, "ip")
    device = service.device(client["client_id"], service.resource)
    now[0] += 590
    with pytest.raises(ValueError, match="authorization_pending"):
        service.device_token(device["device_code"], client["client_id"])
    now[0] += 11
    with pytest.raises(ValueError, match="expired_token"):
        service.device_token(device["device_code"], client["client_id"])


def _browser(service):
    return TestClient(Starlette(routes=auth_routes(service)), base_url="https://mcp.example")


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("get", "/authorize", {"client_id": "nobody", "response_type": "code"}),
        ("post", "/login", {"flow": "gone", "csrf": "stale"}),
        ("post", "/consent", {"flow": "gone", "csrf": "stale", "decision": "approve"}),
        ("get", "/oidc/callback", {"state": "gone", "error": "access_denied"}),
        ("get", "/device/verify", {"user_code": "NOPE-NOPE"}),
        ("get", "/connected-clients", {}),
    ],
)
def test_a_failure_a_person_meets_in_the_browser_is_a_page_not_json(auth, method, path, data):
    service, _, _ = auth
    with _browser(service) as http:
        sent = getattr(http, method)
        response = sent(path, params=data) if method == "get" else sent(path, data=data)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "style-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert "start again from your client" in response.text
    assert '{"error"' not in response.text


def test_a_declined_sign_in_says_so(auth):
    service, _, _ = auth
    with _browser(service) as http:
        response = http.get("/oidc/callback", params={"state": "gone", "error": "access_denied"})
    assert "declined" in response.text


def test_client_endpoints_keep_their_json_errors(auth):
    service, _, _ = auth
    with _browser(service) as http:
        refused = http.post("/token", data={"grant_type": "authorization_code", "code": "x"})
    assert refused.status_code == 400
    assert refused.headers["content-type"] == "application/json"
    assert refused.json()["error"] in {"invalid_client", "invalid_grant", "invalid_request"}


def _set_password_through_the_page(http, token):
    page = http.get("/set-password")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return http.post(
        "/set-password", data={"csrf": csrf, "token": token, "password": "another-long-password"}
    )


def _browser_signed_in(service, http, principal):
    token = "browser-fixture-token"
    service.state.put(
        "browser_sessions", digest(token), {"principal_id": principal["id"]}, expires_at=10**9
    )
    http.cookies.set("vecten_browser", token, domain="mcp.example")
    assert http.get("/connected-clients").status_code == 200


def test_setting_a_password_ends_every_session_the_person_holds(auth):
    service, _, p = auth
    other = service.state.invite("ola@example.com", "Ola", "ola", provisioned=True)
    revoked = []
    service.revoke_session = revoked.append
    client, tokens = issue(auth)
    _, second = issue(auth)
    with _browser(service) as http:
        _browser_signed_in(service, http, p)
        sid = next(s for s, row in service.state.scan("sessions") if row["principal_id"] == p["id"])
        service.state.put(
            "sessions",
            "other-person-session",
            {**service.state.get("sessions", sid), "principal_id": other["id"]},
        )
        assert (
            _set_password_through_the_page(http, service.backend.setup_link(p["email"])).status_code
            == 200
        )
        assert http.get("/connected-clients").status_code == 400
    for access in (tokens["access_token"], second["access_token"]):
        with pytest.raises(ValueError):
            service.resolve(access)
    with pytest.raises(ValueError, match="^invalid_grant$"):
        service.refresh(tokens["refresh_token"], client["client_id"])
    assert len(revoked) == 2 and "other-person-session" not in revoked
    assert service.state.get("sessions", "other-person-session")


def test_a_newer_set_password_link_voids_the_older_one(auth):
    service, _, p = auth
    older = service.backend.setup_link(p["email"])
    newer = service.backend.setup_link(p["email"])
    with _browser(service) as http:
        assert _set_password_through_the_page(http, newer).status_code == 200
        assert _set_password_through_the_page(http, older).status_code == 400
    assert service.backend.authenticate(p["email"], "another-long-password", "ip")
