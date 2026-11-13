"""Sign-in through the server's own authorization code flow, and its session cookie, for the
admin panel under /admin and for the apps page under /apps."""

import secrets
from urllib.parse import urlencode

from vecten_mcp.auth.identity import challenge, digest
from vecten_mcp.auth.service import Resolved

PANEL_CLIENT = "vecten-admin-panel"
SESSION_COOKIE = "vecten_panel"
LOGIN_COOKIE = "vecten_panel_login"
SESSION_SECONDS = 8 * 3600
LOGIN_SECONDS = 600
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
LOOPBACK_REDIRECTS = 4


class PanelSessions:
    def __init__(
        self,
        oauth,
        *,
        base="/admin",
        client_id=PANEL_CLIENT,
        client_name="Vecten admin panel",
        cookie=SESSION_COOKIE,
        login_cookie=LOGIN_COOKIE,
        kind="panel",
    ):
        self.oauth, self.state = oauth, oauth.state
        # Each surface signs in through its own client, cookies and state kinds, so a
        # session on one is never a session on the other.
        self.base, self.client_id, self.client_name = base, client_id, client_name
        self.session_cookie, self.login_cookie = cookie, login_cookie
        self.sessions, self.logins = f"{kind}_sessions", f"{kind}_logins"

    def origin(self, request):
        # A browser reaches the panel either at the issuer or, with /admin off the public
        # listener, through a tunnel to a loopback port; any other Host is not trusted.
        url = request.url
        if url.scheme == "http" and url.hostname in LOOPBACK:
            return f"http://{url.netloc}"
        return self.oauth.issuer

    def client(self, redirect):
        issuer_redirect = self.oauth.issuer + self.base + "/callback"
        client = self.state.get("clients", self.client_id) or dict(
            client_id=self.client_id,
            client_name=self.client_name,
            redirect_uris=[],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code"],
            response_types=["code"],
            created_at=self.oauth.clock(),
            used=True,
        )
        others = [uri for uri in client["redirect_uris"] if uri not in {issuer_redirect, redirect}]
        loopback = [redirect] if redirect != issuer_redirect else []
        uris = [issuer_redirect, *loopback, *others[: LOOPBACK_REDIRECTS - len(loopback)]]
        if uris != client["redirect_uris"]:
            self.state.put("clients", self.client_id, {**client, "redirect_uris": uris})

    def cookie(self, response, name, value, max_age, origin):
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path=self.base,
            secure=origin.startswith("https:"),
            httponly=True,
            samesite="strict",
        )

    def begin(self, request):
        origin = self.origin(request)
        redirect = origin + self.base + "/callback"
        self.client(redirect)
        verifier, state, nonce = (secrets.token_urlsafe(48) for _ in range(3))
        self.state.put(
            self.logins,
            digest(nonce),
            {"verifier": verifier, "state": state, "redirect_uri": redirect},
            expires_at=self.oauth.clock() + LOGIN_SECONDS,
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect,
                "code_challenge": challenge(verifier),
                "code_challenge_method": "S256",
                "resource": self.oauth.resource,
                "state": state,
            }
        )
        return "/authorize?" + query, nonce, origin

    def finish(self, request):
        """Exchange the code and return the caller; raises ValueError on any mismatch."""
        nonce = request.cookies.get(self.login_cookie, "")
        pending = self.state.take(self.logins, digest(nonce), now=self.oauth.clock())
        supplied = request.query_params.get("state", "")
        if not pending or not secrets.compare_digest(supplied, pending["state"]):
            raise ValueError("sign-in expired or was started elsewhere")
        tokens = self.oauth.exchange(
            request.query_params.get("code", ""),
            self.client_id,
            pending["redirect_uri"],
            pending["verifier"],
            self.oauth.resource,
        )
        return self.oauth.resolve(tokens["access_token"])

    def open(self, caller):
        token = secrets.token_urlsafe(32)
        # The access token from the exchange is discarded; the OAuth session bounds the panel.
        session = self.state.get("sessions", caller.session_id) or {}
        ends = session.get("expires_at", self.oauth.clock())
        expires = min(self.oauth.clock() + SESSION_SECONDS, ends)
        self.state.put(
            self.sessions,
            digest(token),
            {
                "sid": caller.session_id,
                "principal_id": caller.principal["id"],
                "csrf": secrets.token_urlsafe(32),
            },
            expires_at=expires,
        )
        return token, int(expires - self.oauth.clock())

    def current(self, request):
        """The caller and session record, re-read from state on every request."""
        key = digest(request.cookies.get(self.session_cookie, ""))
        now = self.oauth.clock()
        record = self.state.get(self.sessions, key, now=now)
        if not record:
            return None
        session = self.state.get("sessions", record["sid"], now=now)
        principal = self.state.principal(record["principal_id"])
        if (
            not session
            or not principal
            or not principal["enabled"]
            or session["principal_id"] != principal["id"]
        ):
            self.state.remove(self.sessions, key)
            return None
        caller = Resolved(
            principal,
            self.state.groups_for(principal["id"]),
            session["client_id"],
            record["sid"],
            session["expires_at"],
        )
        return caller, {**record, "key": key}

    def csrf_ok(self, record, form):
        return secrets.compare_digest(str(form.get("csrf", "")), record["csrf"])

    def close(self, record):
        self.state.remove(self.sessions, record["key"])
        self.oauth.revoke(record["sid"])
