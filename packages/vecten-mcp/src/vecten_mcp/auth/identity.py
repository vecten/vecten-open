"""Identity backends never supply groups or warehouse privileges."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from vecten_mcp.auth.limits import RateLimiter
from vecten_mcp.identity import Identity, warehouse_name

ADMIN_LINK_SECONDS = 900


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def challenge(value):
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).rstrip(b"=").decode()


class PasswordBackend:
    def __init__(self, state, *, clock=time.time, admin_link=None, admins=()):
        """`admin_link` returns the raw MCP_ADMIN_LINK secret or None; it is called on every
        redemption so a link issued after startup is seen without a restart."""
        self.state, self.clock = state, clock
        self.admin_link, self.admins = admin_link, admins
        self.hasher = PasswordHasher()
        self.dummy = self.hasher.hash(secrets.token_urlsafe(24))
        self.principal_limit = RateLimiter(clock=clock)
        self.address_limit = RateLimiter(limit=30, clock=clock)

    def setup_link(self, email):
        principal = self.state.by_email(email)
        if not principal or principal["kind"] != "human" or not principal["enabled"]:
            raise ValueError("person is not available for password setup")
        token = secrets.token_urlsafe(32)
        self.state.put(
            "password_links",
            digest(token),
            {"email": principal["email"], "principal_id": principal["id"]},
            expires_at=self.clock() + 900,
        )
        return token

    def set_password(self, token, password, address=None):
        """Returns the principal's id, whose OAuth sessions the caller revokes.

        `address` counts against the same per-address limit as logins, which bounds the
        MCP_ADMIN_LINK reads an unknown token causes."""
        if address is not None:
            self.address_limit.hit(address)
        if len(password) < 12 or len(password) > 1024:
            raise ValueError("password must contain 12 to 1024 characters")
        hashed = self.hasher.hash(password)
        with self.state.transaction() as conn:
            link = self.state.take("password_links", digest(token), now=self.clock(), conn=conn)
            if link:
                return self._store_password(link, hashed, conn)
        email = self._admin_link_email(token)
        if email is None:
            raise ValueError("password setup link is invalid or expired")
        if not self.state.by_email(email):
            occupied = {p["warehouse_name"] for p in self.state.principals()}
            self.state.invite(email, email, warehouse_name(email.split("@")[0], occupied))
        link = {"email": email, "principal_id": self.state.by_email(email)["id"]}
        with self.state.transaction() as conn:
            if self.state.get("used_admin_links", digest(token), conn=conn, lock=True):
                raise ValueError("password setup link is invalid or expired")
            principal_id = self._store_password(link, hashed, conn)
            self.state.put("used_admin_links", digest(token), {"email": email}, conn=conn)
        return principal_id

    def _admin_link_email(self, token):
        """The admin email an unexpired MCP_ADMIN_LINK grants `token`, else None."""
        raw = self.admin_link() if self.admin_link else None
        if not raw:
            return None
        try:
            doc = json.loads(raw)
            stored, email, issued_at = doc["token"], doc["email"], float(doc["issued_at"])
        except ValueError, TypeError, KeyError:
            return None
        if not isinstance(stored, str) or not isinstance(email, str):
            return None
        email = email.strip().lower()
        if (
            not hmac.compare_digest(digest(token), digest(stored))
            or self.clock() - issued_at >= ADMIN_LINK_SECONDS
            or email not in {admin.lower() for admin in self.admins}
        ):
            return None
        return email

    def _store_password(self, link, hashed, conn):
        principal = self.state.principal(link.get("principal_id"), conn=conn)
        if (
            not principal
            or not principal["enabled"]
            or principal["kind"] != "human"
            or principal["email"] != link["email"]
        ):
            raise ValueError("password setup principal is unavailable")
        self.state.put("passwords", link["email"], {"hash": hashed}, conn=conn)
        # In the hash's transaction, so an older link cannot overwrite the password set now.
        for kind in ("password_links", "browser_sessions"):
            for key, row in self.state.scan(kind, conn=conn):
                if row.get("principal_id") == principal["id"]:
                    self.state.remove(kind, key, conn=conn)
        return principal["id"]

    def authenticate(self, email, password, address):
        email = email.strip().lower()
        self.principal_limit.hit(email)
        self.address_limit.hit(address)
        principal = self.state.by_email(email)
        row = self.state.get("passwords", email)
        try:
            valid = len(password) <= 1024 and self.hasher.verify(
                row["hash"] if row else self.dummy, password
            )
        except VerificationError:
            valid = False
        if not valid or not principal or not principal["enabled"] or principal["kind"] != "human":
            raise ValueError("invalid email or password")
        self.principal_limit.clear(email)
        return Identity("password", email, email, principal["name"])


class OIDCBackend:
    def __init__(
        self,
        state,
        issuer,
        client_id,
        redirect_uri,
        *,
        client_secret=None,
        client=None,
        clock=time.time,
    ):
        self.state, self.issuer, self.client_id = state, issuer, client_id
        self.redirect_uri, self.client_secret = redirect_uri, client_secret
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.clock = clock

    def metadata(self):
        if not self.issuer or not self.client_id or urlparse(self.issuer).scheme != "https":
            raise ValueError("OIDC issuer and client id must be configured before login")
        result = self.client.get(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
        result.raise_for_status()
        metadata = result.json()
        if metadata.get("issuer") != self.issuer:
            raise ValueError("OIDC issuer mismatch")
        if any(
            urlparse(metadata.get(k, "")).scheme != "https"
            for k in ("authorization_endpoint", "token_endpoint", "jwks_uri")
        ):
            raise ValueError("OIDC endpoints must use HTTPS")
        return metadata

    def begin(self, flow):
        metadata = self.metadata()
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        # The verifier is short-lived protocol state, never a bearer credential.
        self.state.put(
            "oidc_states",
            digest(state),
            {"flow": flow, "nonce": nonce, "verifier": verifier},
            expires_at=self.clock() + 600,
        )
        query = dict(
            response_type="code",
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            scope="openid email profile",
            state=state,
            nonce=nonce,
            code_challenge=challenge(verifier),
            code_challenge_method="S256",
        )
        return {
            "state": state,
            "nonce": nonce,
            "url": metadata["authorization_endpoint"] + "?" + urlencode(query),
        }

    def flow_for(self, state):
        """The browser flow an unexpired provider state belongs to, without consuming it."""
        pending = self.state.get("oidc_states", digest(str(state)), now=self.clock())
        return pending["flow"] if pending else None

    def callback(self, state, code):
        pending = self.state.take("oidc_states", digest(state), now=self.clock())
        if not pending:
            raise ValueError("OIDC state is invalid or expired")
        metadata = self.metadata()
        data = dict(
            grant_type="authorization_code",
            code=code,
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            code_verifier=pending["verifier"],
        )
        if self.client_secret:
            data["client_secret"] = self.client_secret
        try:
            response = self.client.post(metadata["token_endpoint"], data=data)
            response.raise_for_status()
            token = response.json()["id_token"]
            jwks = self.client.get(metadata["jwks_uri"])
            jwks.raise_for_status()
            kid = jwt.get_unverified_header(token)["kid"]
            key = next(
                k
                for k in jwks.json()["keys"]
                if k.get("kid") == kid and k.get("use", "sig") == "sig"
            )
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256", "ES256"],
                issuer=self.issuer,
                audience=self.client_id,
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "require": ["iss", "sub", "aud", "exp", "iat", "nonce"],
                },
            )
            if (
                claims["exp"] <= self.clock()
                or claims["iat"] > self.clock() + 30
                or claims["nonce"] != pending["nonce"]
                or claims.get("email_verified") is not True
                or claims.get("azp", self.client_id) != self.client_id
            ):
                raise ValueError("OIDC identity is unverified, expired or misbound")
            return Identity(
                self.issuer, claims["sub"], claims["email"], claims.get("name") or claims["email"]
            ), pending["flow"]
        except (httpx.HTTPError, jwt.PyJWTError, KeyError, StopIteration, TypeError) as exc:
            raise ValueError("OIDC authentication failed") from exc
