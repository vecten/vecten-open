"""OAuth state transitions are transactional; every request rechecks the principal."""

import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

from vecten_mcp.auth.identity import challenge, digest
from vecten_mcp.auth.limits import RateLimiter


@dataclass
class Resolved:
    principal: dict
    groups: set[str]
    client_id: str
    session_id: str
    expires_at: float


# A dynamically registered client may hold a secret, but never the client_credentials grant:
# that belongs to service principals, which only an admin creates.
REGISTERED_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
# The server's maximum session lifetime: an OAuth session and a gate session end by then.
SESSION_SECONDS = 604800


class RegistrationError(ValueError):
    """A refused registration, carrying its RFC 7591 error code and a description."""

    def __init__(self, error, description):
        super().__init__(error)
        self.error, self.description = error, description


def is_admin(caller):
    return caller.principal["kind"] == "human" and caller.principal["admin"]


class OAuthService:
    def __init__(
        self, state, keys, issuer, backend, *, admins=(), clock=time.time, revoke_session=None
    ):
        self.state, self.keys, self.backend = state, keys, backend
        self.issuer, self.clock = issuer.rstrip("/"), clock
        self.resource = self.issuer + "/mcp"
        self.admins = {email.lower() for email in admins}
        self.revoke_session = revoke_session or (lambda sid: None)
        self.registration_limit = RateLimiter(limit=10, window=60, clock=clock)
        self.token_limit = RateLimiter(limit=60, window=60, clock=clock)

    def event(self, principal, operation, subject, outcome):
        self.state.access_event(principal, operation, subject, outcome, now=self.clock())

    def register(self, data, address):
        self.registration_limit.hit(address)
        redirects = data.get("redirect_uris", [])
        if not isinstance(redirects, list) or len(redirects) > 10:
            raise RegistrationError("invalid_redirect_uri", "redirect_uris must list at most 10")
        for uri in redirects:
            parsed = urlparse(uri)
            if (
                parsed.fragment
                or parsed.username
                or parsed.password
                or not parsed.hostname
                or not (
                    parsed.scheme == "https"
                    or (
                        parsed.scheme == "http"
                        and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                    )
                )
            ):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    f"{uri} must be HTTPS, or HTTP on a loopback address, without a fragment",
                )
        method = data.get("token_endpoint_auth_method", "none")
        if method not in REGISTERED_AUTH_METHODS:
            raise RegistrationError(
                "invalid_client_metadata",
                f"token_endpoint_auth_method {method!r} is not one of "
                + ", ".join(REGISTERED_AUTH_METHODS),
            )
        name = data.get("client_name", "MCP client")
        if not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise RegistrationError(
                "invalid_client_metadata", "client_name must be 1-200 characters"
            )
        cid, issued = secrets.token_urlsafe(24), self.clock()
        client = dict(
            client_id=cid,
            client_name=name,
            redirect_uris=redirects,
            token_endpoint_auth_method=method,
            grant_types=[
                "authorization_code",
                "refresh_token",
                "urn:ietf:params:oauth:grant-type:device_code",
            ],
            response_types=["code"],
            client_id_issued_at=int(issued),
        )
        secret = secrets.token_urlsafe(48) if method != "none" else None
        stored = dict(client, created_at=issued, used=False)
        if secret:
            stored["secret_hash"] = digest(secret)
            client |= {"client_secret": secret, "client_secret_expires_at": 0}
        self.state.put("clients", cid, stored)
        return client

    def client(self, client_id):
        client = self.state.get("clients", client_id)
        if not client:
            raise ValueError("invalid_client")
        return client

    def authorize(self, client_id, redirect_uri, code_challenge, method, resource, state):
        client = self.client(client_id)
        if client.get("principal_id") or redirect_uri not in client["redirect_uris"]:
            raise ValueError("invalid redirect URI or client")
        if method != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge):
            raise ValueError("PKCE S256 is required")
        if resource != self.resource:
            raise ValueError("invalid_target")
        flow = secrets.token_urlsafe(32)
        self.state.put(
            "flows",
            digest(flow),
            dict(
                client_id=client_id,
                redirect_uri=redirect_uri,
                challenge=code_challenge,
                resource=resource,
                state=state,
            ),
            expires_at=self.clock() + 600,
        )
        return flow

    def login_identity(self, identity):
        principal = self.state.by_email(identity.email)
        if principal is None and identity.email in self.admins:
            from vecten_mcp.identity import warehouse_name

            name = warehouse_name(
                identity.name, {p["warehouse_name"] for p in self.state.principals()}
            )
            principal = self.state.invite(identity.email, identity.name, name)
        first_login = principal is not None and principal["issuer"] is None
        principal = self.state.bind_identity(identity)
        if first_login and identity.email in self.admins:
            principal = self.state.update_principal(principal["id"], admin=True)
        self.event(principal["id"], "login", identity.email, "allowed")
        return principal

    def authenticate(self, flow, identity):
        principal = self.login_identity(identity)
        with self.state.transaction() as conn:
            pending = self.state.get("flows", digest(flow), now=self.clock(), conn=conn, lock=True)
            if not pending:
                raise ValueError("authorization flow expired")
            pending["principal_id"] = principal["id"]
            self.state.put("flows", digest(flow), pending, expires_at=self.clock() + 600, conn=conn)
        return principal

    def consent(self, flow, approve):
        with self.state.transaction() as conn:
            pending = self.state.take("flows", digest(flow), now=self.clock(), conn=conn)
            if not pending or not pending.get("principal_id"):
                raise ValueError("authenticated authorization flow required")
            if pending.get("device_user_code"):
                link = self.state.take(
                    "device_users", digest(pending["device_user_code"]), now=self.clock(), conn=conn
                )
                device = (
                    self.state.get(
                        "devices", link["device"], now=self.clock(), conn=conn, lock=True
                    )
                    if link
                    else None
                )
                if not device:
                    raise ValueError("device code expired")
                device.update(principal_id=pending["principal_id"], approved=approve)
                self.state.put(
                    "devices", link["device"], device, expires_at=device["expires_at"], conn=conn
                )
                return "device_complete"
            query = {"state": pending["state"]}
            if approve:
                code = secrets.token_urlsafe(32)
                self.state.put(
                    "codes", digest(code), pending, expires_at=self.clock() + 120, conn=conn
                )
                query["code"] = code
            else:
                query["error"] = "access_denied"
        self.event(
            pending["principal_id"],
            "consent",
            pending["client_id"],
            "allowed" if approve else "denied",
        )
        return (
            pending["redirect_uri"]
            + ("&" if "?" in pending["redirect_uri"] else "?")
            + urlencode(query)
        )

    def _session(self, principal_id, client_id, conn):
        principal = self.state.principal(principal_id, conn=conn)
        if not principal or not principal["enabled"]:
            raise ValueError("principal disabled")
        sid = secrets.token_urlsafe(24)
        session = dict(
            principal_id=principal_id,
            client_id=client_id,
            started_at=self.clock(),
            expires_at=self.clock() + SESSION_SECONDS,
        )
        self.state.put("sessions", sid, session, expires_at=session["expires_at"], conn=conn)
        client = self.state.get("clients", client_id, conn=conn)
        client["used"] = True
        self.state.put("clients", client_id, client, conn=conn)
        return sid, session

    def _tokens(self, sid, session, conn, *, refresh=True):
        expiry = min(self.clock() + 3600, session["expires_at"])
        access = self.keys.sign(
            session["principal_id"],
            self.resource,
            expiry,
            extra={"sid": sid, "client_id": session["client_id"]},
        )
        self.state.put("access", digest(access), {"sid": sid}, expires_at=expiry, conn=conn)
        result = dict(
            access_token=access,
            token_type="Bearer",
            expires_in=int(expiry - self.clock()),
            scope="mcp",
        )
        if refresh:
            token = secrets.token_urlsafe(32)
            self.state.put(
                "refresh",
                digest(token),
                {"sid": sid, "client_id": session["client_id"]},
                expires_at=session["expires_at"],
                conn=conn,
            )
            result["refresh_token"] = token
        return result

    def _authenticate(self, client_id, secret):
        """Check a registered confidential client's secret; a public client presents none."""
        client = self.state.get("clients", client_id)
        expected = client.get("secret_hash") if client else None
        if expected and not secrets.compare_digest(expected, digest(secret or "")):
            raise ValueError("invalid_client")

    def exchange(self, code, client_id, redirect_uri, verifier, resource, secret=""):
        self._authenticate(client_id, secret)
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
            raise ValueError("invalid_grant")
        with self.state.transaction() as conn:
            pending = self.state.take("codes", digest(code), now=self.clock(), conn=conn)
            if (
                not pending
                or pending["client_id"] != client_id
                or pending["redirect_uri"] != redirect_uri
                or resource != self.resource
                or not secrets.compare_digest(challenge(verifier), pending["challenge"])
            ):
                raise ValueError("invalid_grant")
            sid, session = self._session(pending["principal_id"], client_id, conn)
            return self._tokens(sid, session, conn)

    def refresh(self, token, client_id, secret=""):
        self._authenticate(client_id, secret)
        replay = self.state.get("used_refresh", digest(token), now=self.clock())
        # Every refusal is invalid_grant, the one error on which an OAuth client authorizes
        # again; a replay has already revoked the session it belonged to.
        if replay and replay["client_id"] == client_id:
            self.revoke(replay["sid"])
            raise ValueError("invalid_grant")
        with self.state.transaction() as conn:
            row = self.state.take("refresh", digest(token), now=self.clock(), conn=conn)
            if not row or row["client_id"] != client_id:
                raise ValueError("invalid_grant")
            session = self.state.get("sessions", row["sid"], now=self.clock(), conn=conn)
            principal = (
                self.state.principal(session["principal_id"], conn=conn) if session else None
            )
            if not session or not principal or not principal["enabled"]:
                raise ValueError("invalid_grant")
            self.state.put(
                "used_refresh", digest(token), row, expires_at=session["expires_at"], conn=conn
            )
            return self._tokens(row["sid"], session, conn)

    def resolve(self, token, audience=None):
        claims = self.keys.verify(token, audience or self.resource)
        record = self.state.get("access", digest(token), now=self.clock())
        session = self.state.get("sessions", claims.get("sid", ""), now=self.clock())
        principal = self.state.principal(claims["sub"])
        if (
            not record
            or not session
            or not principal
            or not principal["enabled"]
            or record["sid"] != claims["sid"]
            or session["principal_id"] != principal["id"]
            or session["client_id"] != claims.get("client_id")
        ):
            raise ValueError("invalid_token")
        return Resolved(
            principal,
            self.state.groups_for(principal["id"]),
            session["client_id"],
            claims["sid"],
            min(claims["exp"], session["expires_at"]),
        )

    def revoke(self, sid):
        session = self.state.get("sessions", sid)
        self.state.remove("sessions", sid)
        self.revoke_session(sid)
        if session:
            self.event(session["principal_id"], "revoke", session["client_id"], "allowed")

    def revoke_client(self, principal_id, client_id):
        for sid, session in self.state.scan("sessions"):
            if session["principal_id"] == principal_id and session["client_id"] == client_id:
                self.revoke(sid)

    def revoke_principal(self, principal_id):
        self.state.end_gate_sessions(principal_id)
        for sid, session in self.state.scan("sessions"):
            if session["principal_id"] == principal_id:
                self.revoke(sid)

    def disable_principal(self, principal_id):
        self.state.update_principal(principal_id, enabled=False)
        self.revoke_principal(principal_id)

    def service_credentials(self, principal_id):
        principal = self.state.principal(principal_id)
        if not principal or principal["kind"] != "service":
            raise ValueError("human principals authenticate through the authorization code flow")
        cid, secret = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        self.state.put(
            "clients",
            cid,
            dict(
                client_id=cid,
                client_name=principal["name"],
                principal_id=principal_id,
                secret_hash=digest(secret),
                used=True,
                redirect_uris=[],
            ),
        )
        return dict(client_id=cid, client_secret=secret)

    def replace_service_credentials(self, principal_id):
        """New client credentials for a service principal; every earlier client of it and
        every session they opened stop working first."""
        for client_id, client in self.state.scan("clients"):
            if client.get("principal_id") == principal_id:
                self.state.remove("clients", client_id)
        self.revoke_principal(principal_id)
        return self.service_credentials(principal_id)

    def client_credentials(self, client_id, secret, resource):
        client = self.client(client_id)
        if (
            resource != self.resource
            or not client.get("principal_id")
            or not secrets.compare_digest(client.get("secret_hash", ""), digest(secret))
        ):
            raise ValueError("invalid_client; humans use authorization code flow")
        principal = self.state.principal(client["principal_id"])
        if not principal or principal["kind"] != "service" or not principal["enabled"]:
            raise ValueError("invalid_client")
        with self.state.transaction() as conn:
            sid, session = self._session(principal["id"], client_id, conn)
            session["expires_at"] = self.clock() + 3600
            self.state.put("sessions", sid, session, expires_at=session["expires_at"], conn=conn)
            return self._tokens(sid, session, conn, refresh=False)

    def device(self, client_id, resource):
        client = self.client(client_id)
        if client.get("principal_id") or resource != self.resource:
            raise ValueError("invalid_client or invalid_target")
        code = secrets.token_urlsafe(32)
        user_code = secrets.token_hex(4).upper()
        row = dict(
            client_id=client_id,
            user_code=user_code,
            last_poll=0,
            interval=5,
            expires_at=self.clock() + 600,
        )
        self.state.put("devices", digest(code), row, expires_at=self.clock() + 600)
        self.state.put(
            "device_users",
            digest(user_code),
            {"device": digest(code)},
            expires_at=self.clock() + 600,
        )
        return dict(
            device_code=code,
            user_code=user_code,
            verification_uri=self.issuer + "/device/verify",
            verification_uri_complete=self.issuer
            + "/device/verify?"
            + urlencode({"user_code": user_code}),
            expires_in=600,
            interval=5,
        )

    def approve_device(self, user_code, identity, approve):
        principal = self.login_identity(identity)
        with self.state.transaction() as conn:
            link = self.state.take(
                "device_users", digest(user_code.upper()), now=self.clock(), conn=conn
            )
            device = (
                self.state.get("devices", link["device"], now=self.clock(), conn=conn, lock=True)
                if link
                else None
            )
            if not device:
                raise ValueError("device code expired")
            device.update(principal_id=principal["id"], approved=approve)
            self.state.put(
                "devices", link["device"], device, expires_at=device["expires_at"], conn=conn
            )
        self.event(
            principal["id"],
            "device_consent",
            device["client_id"],
            "allowed" if approve else "denied",
        )

    def device_token(self, code, client_id):
        error = None
        with self.state.transaction() as conn:
            device = self.state.get("devices", digest(code), now=self.clock(), conn=conn, lock=True)
            if not device or device["client_id"] != client_id:
                raise ValueError("expired_token")
            if device["last_poll"] and self.clock() < device["last_poll"] + device["interval"]:
                device["interval"] += 5
                error = "slow_down"
            elif "approved" not in device:
                error = "authorization_pending"
            elif not device["approved"]:
                error = "access_denied"
            if error:
                device["last_poll"] = self.clock()
                self.state.put(
                    "devices", digest(code), device, expires_at=device["expires_at"], conn=conn
                )
            else:
                self.state.remove("devices", digest(code), conn=conn)
                sid, session = self._session(device["principal_id"], client_id, conn)
                return self._tokens(sid, session, conn)
        raise ValueError(error)

    def maintain(self):
        self.state.prune(self.clock())
        for cid, client in self.state.scan("clients"):
            if not client.get("used") and client.get("created_at", 0) + 86400 <= self.clock():
                self.state.remove("clients", cid)
