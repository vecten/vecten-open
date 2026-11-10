"""Private signing material lives only in the provider; retired keys are public."""

import json
import time
from threading import RLock
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from vecten_shared.output import ExitCode, Failure

SIGNING_SECRET = "MCP_SIGNING_KEY"
ROTATE_AFTER = 30 * 86400
# Snowflake caches an External OAuth JWKS per URL and fetches no key it has not seen, so the
# next key is published this long before it signs; the window must outlast that cache.
PUBLISH_AHEAD = 7 * 86400
# A retired key stays published as long as the tokens it signed can live.
TOKEN_LIFETIME = 3600


class SigningKeys:
    def __init__(self, provider, issuer, *, clock=time.time):
        self.provider, self.issuer, self.clock = provider, issuer.rstrip("/"), clock
        self.lock = RLock()
        self.ring = {"previous": []}
        try:
            self.ring = json.loads(provider.get_secret(SIGNING_SECRET))
        except KeyError:
            pass
        except Failure as exc:
            if exc.code != ExitCode.PREREQUISITE:
                raise
        if "private" not in self.ring:
            self.rotate()

    def rotate(self):
        """Sign with the published next key, or a new one when none is published."""
        with self.lock:
            now = self.clock()
            previous = [k for k in self.ring["previous"] if k["until"] > now]
            if self.ring.get("private"):
                previous.append({"jwk": self._public(self.ring), "until": now + TOKEN_LIFETIME})
            upcoming = self.ring.get("next") or _new_key(now)
            ring = dict(
                kid=upcoming["kid"],
                private=upcoming["private"],
                created_at=now,
                previous=previous,
            )
            self.provider.put_secret("MCP_SIGNING_KEY", json.dumps(ring))
            self.ring = ring

    def maintain(self):
        """Publish the next key a window before the current one is due, then switch to it."""
        with self.lock:
            now = self.clock()
            upcoming = self.ring.get("next")
            if upcoming is None:
                if now - self.ring["created_at"] >= ROTATE_AFTER - PUBLISH_AHEAD:
                    ring = {**self.ring, "next": _new_key(now)}
                    self.provider.put_secret("MCP_SIGNING_KEY", json.dumps(ring))
                    self.ring = ring
            elif now - upcoming["published_at"] >= PUBLISH_AHEAD:
                self.rotate()

    @staticmethod
    def _public(entry):
        key = serialization.load_pem_private_key(entry["private"].encode(), password=None)
        return {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
            "kid": entry["kid"],
            "use": "sig",
            "alg": "RS256",
        }

    def jwks(self):
        with self.lock:
            upcoming = [self._public(self.ring["next"])] if self.ring.get("next") else []
            return {
                "keys": [
                    self._public(self.ring),
                    *upcoming,
                    *(k["jwk"] for k in self.ring["previous"] if k["until"] > self.clock()),
                ]
            }

    def sign(self, subject, audience, expires_at, *, extra=None):
        with self.lock:
            now = int(self.clock())
            expiry = min(int(expires_at), now + TOKEN_LIFETIME)
            if expiry <= now:
                raise ValueError("cannot issue expired token")
            claims = {
                "iss": self.issuer,
                "sub": subject,
                "aud": audience,
                "iat": now,
                "exp": expiry,
                "jti": uuid4().hex,
            }
            claims.update(extra or {})
            return jwt.encode(
                claims, self.ring["private"], algorithm="RS256", headers={"kid": self.ring["kid"]}
            )

    def verify(self, token, audience):
        try:
            kid = jwt.get_unverified_header(token)["kid"]
            key = next(k for k in self.jwks()["keys"] if k["kid"] == kid)
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=["RS256"],
                audience=audience,
                issuer=self.issuer,
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "require": ["exp", "iat", "sub", "iss", "aud", "jti"],
                },
            )
            if (
                claims["exp"] <= self.clock()
                or claims["iat"] > self.clock() + 30
                or claims["exp"] - claims["iat"] > TOKEN_LIFETIME
            ):
                raise ValueError("token expired or invalid lifetime")
            return claims
        except (jwt.PyJWTError, KeyError, StopIteration, TypeError) as exc:
            raise ValueError("invalid token") from exc

    def snowflake_token(self, subject, roles, expires_at, audience):
        # A token scoped to one role cannot be used under any other. Several group roles need
        # USE SECONDARY ROLES, which Snowflake allows in an External OAuth session only under
        # session:role-any; the connection then activates exactly those roles.
        if len(roles) == 1:
            scope = "session:role:" + next(iter(roles))
        else:
            scope = "session:role-any"
        return self.sign(subject, audience, expires_at, extra={"scope": scope})


def _new_key(now):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {
        "kid": uuid4().hex,
        "private": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "published_at": now,
    }
