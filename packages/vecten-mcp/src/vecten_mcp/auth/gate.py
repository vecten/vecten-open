"""The gate: the server's sign-in admits people to service hostnames, and NGINX asks it about
every request, so the person's current groups decide each one.

NGINX on a service hostname forwards /_vecten/gate/start, /callback and /logout here as
/gate/start, /gate/callback and /gate/logout, and each `login` request's check as /gate/verify,
naming the hostname in X-Vecten-Gate-Host. None of these trusts that header for anything a
secret does not also prove: a code needs the verifier from the starting browser's state cookie,
and verify answers only for the session id in the gate cookie.
"""

import hmac
import re
import secrets
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from vecten_mcp.auth.identity import challenge, digest
from vecten_mcp.auth.service import SESSION_SECONDS

GATE_COOKIE = "__Host-vecten-gate"
# __Host- keeps a sibling hostname from setting it for the whole domain; NGINX strips it from
# what a service receives, as it strips the gate cookie.
STATE_COOKIE = "__Host-vecten-gate-state"
STATE_SECONDS = 600
CODE_SECONDS = 60
HOST_HEADER = "x-vecten-gate-host"
ROUTE_HEADER = "x-vecten-gate-route"
CALLBACK_PATH = "/_vecten/gate/callback"
# A path on the service hostname: one leading slash (a second, or a backslash, makes browsers
# read another host) and visible ASCII only, as NGINX's $request_uri holds it.
RETURN_PATH = re.compile(r"/(?![/\\])[\x21-\x7e]*")
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
VERIFIER = re.compile(r"[A-Za-z0-9_-]{43}")
# Header values are visible ASCII; anything else, and `%` itself, is UTF-8 percent-encoded.
HEADER_SAFE = "".join(chr(c) for c in range(0x20, 0x7F) if chr(c) != "%")


@dataclass(frozen=True)
class GateRoute:
    host: str
    groups: tuple[str, ...]


def header_value(value: str) -> str:
    return quote(value, safe=HEADER_SAFE)


class GateService:
    def __init__(self, oauth, routes: dict[str, GateRoute], *, port: str = ""):
        self.oauth, self.state = oauth, oauth.state
        self.routes = dict(routes)
        self.hosts = frozenset(route.host for route in self.routes.values())
        self.port = port

    @classmethod
    def from_config(cls, oauth, config):
        """Every `login` route vecten.toml binds, at its hostname below the issuer's domain."""
        from vecten_shared.config import service_hostname

        name, colon, port = urlsplit(oauth.issuer).netloc.partition(":")
        if not name.startswith("mcp."):
            # Service hostnames sit beside mcp.<domain>; without that name none can be known.
            return cls(oauth, {})
        domain = name.removeprefix("mcp.")
        routes = {}
        for extension, bindings in config.services.items():
            for service, binding in bindings.items():
                if binding.hostname is None:
                    continue
                host = service_hostname(binding.hostname, domain)
                for route, bound in binding.routes.items():
                    if bound.access == "login":
                        routes[f"{extension}.{service}.{route}"] = GateRoute(
                            host, tuple(bound.groups)
                        )
        return cls(oauth, routes, port=colon + port)

    def origin(self, host):
        return f"https://{host}{self.port}"

    def check(self, host, code_challenge, rd):
        """The pending sign-in for `host`; refuses a hostname no login route binds and a
        return path that is not a path on it."""
        if host not in self.hosts:
            raise ValueError("no login route is bound on that hostname")
        if not isinstance(rd, str) or not RETURN_PATH.fullmatch(rd):
            raise ValueError("the return path must be a path on the service hostname")
        if not isinstance(code_challenge, str) or not CHALLENGE.fullmatch(code_challenge):
            raise ValueError("invalid gate challenge")
        return {"host": host, "challenge": code_challenge, "rd": rd}

    def start(self, host, rd, verifier):
        pending = self.check(host, challenge(verifier), rd)
        return self.oauth.issuer + "/gate/authorize?" + urlencode(pending)

    def flow(self, pending):
        """A browser sign-in that ends in a code for the service instead of consent."""
        flow = secrets.token_urlsafe(32)
        self.state.put(
            "flows", digest(flow), {"gate": pending}, expires_at=self.oauth.clock() + 600
        )
        return flow

    def finish(self, flow, principal):
        pending = self.state.take("flows", digest(flow), now=self.oauth.clock())
        if not pending or not pending.get("gate") or pending.get("principal_id") != principal["id"]:
            raise ValueError("authenticated gate flow required")
        return self.issue(principal["id"], pending["gate"])

    def issue(self, principal_id, pending):
        """The callback URL carrying a one-time code for this person, host and challenge."""
        principal = self.state.principal(principal_id)
        if not principal or not principal["enabled"] or principal["kind"] != "human":
            raise ValueError("access_denied")
        if not principal["provisioned"]:
            raise ValueError("not_provisioned")
        code = secrets.token_urlsafe(32)
        self.state.put(
            "gate_codes",
            digest(code),
            {**pending, "principal_id": principal["id"]},
            expires_at=self.oauth.clock() + CODE_SECONDS,
        )
        self.oauth.event(principal["id"], "gate_sign_in", pending["host"], "allowed")
        return self.origin(pending["host"]) + CALLBACK_PATH + "?" + urlencode({"code": code})

    def redeem(self, code, host, verifier):
        """A new session id and the return path; the code is spent whether or not it passes."""
        now = self.oauth.clock()
        pending = self.state.take("gate_codes", digest(str(code)), now=now)
        if (
            not pending
            or pending["host"] != host
            or not VERIFIER.fullmatch(verifier or "")
            or not secrets.compare_digest(challenge(verifier), pending["challenge"])
        ):
            raise ValueError("gate code is invalid, spent or for another browser")
        principal = self.state.principal(pending["principal_id"])
        if not principal or not principal["enabled"] or principal["kind"] != "human":
            raise ValueError("access_denied")
        sid = secrets.token_urlsafe(32)
        self.state.open_gate_session(
            digest(sid), principal["id"], host, now=now, expires_at=now + SESSION_SECONDS
        )
        return sid, pending["rd"]

    def verify(self, sid, host, route):
        """(status, headers): 401 without a live session on `host`, 403 when the person is in
        none of the route's groups, else 200 with the five identity headers."""
        session = self.state.gate_session(digest(sid), now=self.oauth.clock()) if sid else None
        principal = self.state.principal(session["principal_id"]) if session else None
        if (
            not session
            or session["hostname"] != host
            or not principal
            or not principal["enabled"]
            or principal["kind"] != "human"
        ):
            return 401, {}
        bound = self.routes.get(route)
        groups = self.state.groups_for(principal["id"])
        if bound is None or bound.host != host or not groups & set(bound.groups):
            return 403, {}
        return 200, {
            "X-Vecten-User": header_value(principal["email"]),
            "X-Vecten-Principal": header_value(principal["id"]),
            "X-Vecten-Name": header_value(principal["name"]),
            "X-Vecten-Groups": ",".join(sorted(groups)),
            "X-Vecten-Admin": "true" if principal["admin"] else "false",
        }

    @staticmethod
    def logout_token(sid):
        """The sign-out form's token: only a page the session's own browser read holds it."""
        return digest("gate-logout:" + sid) if sid else ""

    def logout(self, sid, token):
        """Ends the gate session and the person's sign-ins on the server's own pages, so the
        next sign-in asks for credentials; refuses a token the session's page did not hold."""
        if not sid:
            return
        if not hmac.compare_digest(str(token), self.logout_token(sid)):
            raise ValueError("invalid browser state")
        session = self.state.gate_session(digest(sid), now=self.oauth.clock())
        self.state.end_gate_session(digest(sid))
        if session:
            for key, row in self.state.scan("browser_sessions"):
                if row.get("principal_id") == session["principal_id"]:
                    self.state.remove("browser_sessions", key)
