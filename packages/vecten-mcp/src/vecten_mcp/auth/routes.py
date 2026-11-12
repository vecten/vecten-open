"""Browser and OAuth endpoints; authentication failures never echo supplied credentials."""

import base64
import logging
import secrets
from html import escape
from urllib.parse import urlencode

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from vecten_mcp.auth.gate import (
    GATE_COOKIE,
    HOST_HEADER,
    ROUTE_HEADER,
    STATE_COOKIE,
    STATE_SECONDS,
)
from vecten_mcp.auth.identity import OIDCBackend, digest
from vecten_mcp.auth.limits import RateLimit
from vecten_mcp.auth.pages import (
    consent_page,
    failure_page,
    hidden,
    login_page,
    page,
    redirect_page,
)
from vecten_mcp.auth.service import SESSION_SECONDS, RegistrationError
from vecten_mcp.failures import describe_error

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
logger = logging.getLogger(__name__)


# Routes a person reaches in a browser: a failure there is a page, while the client endpoints
# answer with OAuth's JSON error codes.
BROWSER = frozenset(
    {
        "/authorize",
        "/login",
        "/consent",
        "/device/verify",
        "/oidc/start",
        "/oidc/callback",
        "/connected-clients",
        "/set-password",
        "/gate/start",
        "/gate/authorize",
        "/gate/callback",
        "/gate/logout",
    }
)


def auth_routes(service, gate=None):
    state = service.state

    def loopback_origin(request):
        url = request.url
        return f"http://{url.netloc}" if url.scheme == "http" and url.hostname in LOOPBACK else None

    def secure(request):
        # Through a tunnel to a loopback port the browser speaks plain HTTP, and some browsers
        # drop Secure cookies there; every other origin is the HTTPS issuer behind NGINX.
        return service.issuer.startswith("https:") and not loopback_origin(request)

    def address(request):
        return request.client.host if request.client else "unknown"

    def csrf_check(request, form):
        supplied = str(form.get("csrf", ""))
        cookie = request.cookies.get("vecten_csrf", "")
        flow = state.get("browser_flows", digest(str(form.get("flow", ""))), now=service.clock())
        if (
            not cookie
            or not secrets.compare_digest(supplied, cookie)
            or not flow
            or flow["csrf"] != digest(cookie)
        ):
            raise ValueError("invalid browser state")

    def browser_session(request, response, principal):
        token = secrets.token_urlsafe(32)
        state.put(
            "browser_sessions",
            digest(token),
            {"principal_id": principal["id"]},
            expires_at=service.clock() + 3600,
        )
        response.set_cookie(
            "vecten_browser",
            token,
            secure=secure(request),
            httponly=True,
            samesite="lax",
            max_age=3600,
        )
        return response

    def begin_browser(request, flow):
        csrf = secrets.token_urlsafe(32)
        state.put(
            "browser_flows", digest(flow), {"csrf": digest(csrf)}, expires_at=service.clock() + 600
        )
        response = login_page(flow, csrf, oidc=isinstance(service.backend, OIDCBackend))
        response.set_cookie(
            "vecten_csrf",
            csrf,
            secure=secure(request),
            httponly=True,
            samesite="lax",
            max_age=600,
        )
        return response

    async def metadata(request):
        return JSONResponse(
            dict(
                issuer=service.issuer,
                authorization_endpoint=service.issuer + "/authorize",
                token_endpoint=service.issuer + "/token",
                registration_endpoint=service.issuer + "/register",
                revocation_endpoint=service.issuer + "/revoke",
                device_authorization_endpoint=service.issuer + "/device",
                jwks_uri=service.issuer + "/jwks",
                response_types_supported=["code"],
                grant_types_supported=[
                    "authorization_code",
                    "refresh_token",
                    "client_credentials",
                    "urn:ietf:params:oauth:grant-type:device_code",
                ],
                code_challenge_methods_supported=["S256"],
                token_endpoint_auth_methods_supported=[
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                scopes_supported=["mcp"],
            )
        )

    async def resource(request):
        return JSONResponse(
            dict(
                resource=service.resource,
                authorization_servers=[service.issuer],
                bearer_methods_supported=["header"],
                scopes_supported=["mcp"],
            )
        )

    async def register(request):
        return JSONResponse(
            service.register(await request.json(), address(request)), status_code=201
        )

    async def authorize(request):
        q = request.query_params
        if q.get("response_type") != "code":
            raise ValueError("unsupported_response_type")
        flow = service.authorize(
            q.get("client_id"),
            q.get("redirect_uri"),
            q.get("code_challenge", ""),
            q.get("code_challenge_method"),
            q.get("resource"),
            q.get("state", ""),
        )
        return begin_browser(request, flow)

    async def login(request):
        form = await request.form()
        csrf_check(request, form)
        if isinstance(service.backend, OIDCBackend):
            raise ValueError("use the configured identity provider")
        try:
            identity = service.backend.authenticate(
                str(form.get("email", "")), str(form.get("password", "")), address(request)
            )
            principal = service.authenticate(form["flow"], identity)
        except ValueError:
            service.event(None, "login", None, "denied")
            return login_page(
                form["flow"],
                form["csrf"],
                error="Sign-in failed. Check your credentials or try again later.",
            )
        pending = state.get("flows", digest(form["flow"]), now=service.clock())
        if pending and pending.get("gate"):
            return browser_session(request, finish_gate(form["flow"], principal), principal)
        return browser_session(
            request,
            consent_page(
                form["flow"], form["csrf"], service.client(pending["client_id"])["client_name"]
            ),
            principal,
        )

    async def consent(request):
        form = await request.form()
        csrf_check(request, form)
        result = service.consent(form["flow"], form.get("decision") == "approve")
        state.remove("browser_flows", digest(form["flow"]))
        if result == "device_complete":
            return page("Device authorization complete", "<p>You may return to the CLI.</p>")
        return redirect_page(result)

    async def oidc_start(request):
        form = await request.form()
        csrf_check(request, form)
        if not isinstance(service.backend, OIDCBackend):
            raise ValueError("OIDC is not configured")
        origin = loopback_origin(request)
        if origin:
            # The provider returns to the issuer's callback, which cannot read this origin's
            # cookies; the flow records where its browser state lives.
            key = digest(str(form["flow"]))
            started = state.get("browser_flows", key, now=service.clock())
            state.put(
                "browser_flows",
                key,
                {**started, "origin": origin},
                expires_at=service.clock() + 600,
            )
        pending = service.backend.begin(form["flow"])
        return redirect_page(pending["url"])

    async def oidc_callback(request):
        returned = request.query_params
        flow = (
            service.backend.flow_for(returned.get("state", ""))
            if isinstance(service.backend, OIDCBackend)
            else None
        )
        started = (
            state.get("browser_flows", digest(flow), now=service.clock()) if flow else None
        ) or {}
        if started.get("origin") and started["origin"] != loopback_origin(request):
            # The state and code stay unconsumed until the origin holding the flow's cookie
            # runs the usual browser-state check.
            query = urlencode({k: returned[k] for k in ("state", "code", "error") if k in returned})
            return RedirectResponse(started["origin"] + "/oidc/callback?" + query, status_code=303)
        if returned.get("error"):
            # The provider sends no code when the person or its policy declined.
            raise ValueError("access_denied")
        identity, flow = service.backend.callback(
            request.query_params.get("state", ""), request.query_params.get("code", "")
        )
        csrf = request.cookies.get("vecten_csrf", "")
        csrf_check(request, {"flow": flow, "csrf": csrf})
        principal = service.authenticate(flow, identity)
        pending = state.get("flows", digest(flow), now=service.clock())
        if pending and pending.get("gate"):
            # The provider's redirect is a GET, so no form-action rule covers this one.
            url = gate.finish(flow, principal)
            state.remove("browser_flows", digest(flow))
            return browser_session(request, RedirectResponse(url, status_code=303), principal)
        return browser_session(
            request,
            consent_page(flow, csrf, service.client(pending["client_id"])["client_name"]),
            principal,
        )

    async def token(request):
        service.token_limit.hit(address(request))
        form = await request.form()
        cid, secret = form.get("client_id", ""), form.get("client_secret", "")
        if request.headers.get("authorization", "").startswith("Basic "):
            try:
                cid, secret = (
                    base64.b64decode(request.headers["authorization"][6:], validate=True)
                    .decode()
                    .split(":", 1)
                )
            except (ValueError, UnicodeError) as exc:
                raise ValueError("invalid_client") from exc
        grant = form.get("grant_type")
        if grant == "authorization_code":
            result = service.exchange(
                form.get("code", ""),
                cid,
                form.get("redirect_uri"),
                form.get("code_verifier", ""),
                form.get("resource"),
                secret,
            )
        elif grant == "refresh_token":
            result = service.refresh(form.get("refresh_token", ""), cid, secret)
        elif grant == "client_credentials":
            result = service.client_credentials(cid, secret, form.get("resource"))
        elif grant == "urn:ietf:params:oauth:grant-type:device_code":
            result = service.device_token(form.get("device_code", ""), cid)
        else:
            raise ValueError("unsupported_grant_type")
        return JSONResponse(result)

    async def device(request):
        service.token_limit.hit(address(request))
        form = await request.form()
        return JSONResponse(service.device(form.get("client_id"), form.get("resource")))

    async def verify_device(request):
        user_code = request.query_params.get("user_code", "")
        if not user_code:
            return page(
                "Connect your CLI",
                '<form method="get"><label>Device code <input name="user_code" required></label>'
                "<button>Continue</button></form>",
            )
        link = state.get("device_users", digest(user_code.upper()), now=service.clock())
        pending = state.get("devices", link["device"], now=service.clock()) if link else None
        if not pending:
            raise ValueError("device code expired")
        flow = secrets.token_urlsafe(32)
        state.put(
            "flows",
            digest(flow),
            {"client_id": pending["client_id"], "device_user_code": user_code.upper()},
            expires_at=service.clock() + 600,
        )
        return begin_browser(request, flow)

    def browser_principal(request):
        row = state.get(
            "browser_sessions",
            digest(request.cookies.get("vecten_browser", "")),
            now=service.clock(),
        )
        principal = state.principal(row["principal_id"]) if row else None
        return principal if principal and principal["enabled"] else None

    def current_browser(request):
        principal = browser_principal(request)
        if not principal:
            raise ValueError("sign in through an authorization flow first")
        return principal

    def finish_gate(flow, principal):
        # The form posted to this origin; CSP form-action would stop an HTTP redirect from it
        # to the service's hostname.
        url = gate.finish(flow, principal)
        state.remove("browser_flows", digest(flow))
        return redirect_page(url)

    def gate_cookie(response, name, value, *, max_age):
        response.set_cookie(
            name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax"
        )

    def gate_cookie_cleared(response, name):
        response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")

    async def gate_start(request):
        # NGINX appends the visited $request_uri after `rd=` unencoded, so the rest of the
        # query string is the return path, `&` and `?` included.
        raw = request.scope.get("query_string", b"").decode("latin-1")
        if raw and not raw.startswith("rd="):
            raise ValueError("invalid gate start")
        rd = raw.removeprefix("rd=") or "/"
        # Always a new verifier: one the browser sent may have been planted by whoever wants
        # their own code redeemed here.
        verifier = secrets.token_urlsafe(32)
        response = RedirectResponse(
            gate.start(request.headers.get(HOST_HEADER, ""), rd, verifier), status_code=303
        )
        gate_cookie(response, STATE_COOKIE, verifier, max_age=STATE_SECONDS)
        return response

    async def gate_authorize(request):
        query = request.query_params
        pending = gate.check(query.get("host", ""), query.get("challenge", ""), query.get("rd"))
        principal = browser_principal(request)
        if principal:
            return RedirectResponse(gate.issue(principal["id"], pending), status_code=303)
        return begin_browser(request, gate.flow(pending))

    async def gate_callback(request):
        sid, rd = gate.redeem(
            request.query_params.get("code", ""),
            request.headers.get(HOST_HEADER, ""),
            request.cookies.get(STATE_COOKIE, ""),
        )
        response = RedirectResponse(rd, status_code=303)
        gate_cookie(response, GATE_COOKIE, sid, max_age=SESSION_SECONDS)
        gate_cookie_cleared(response, STATE_COOKIE)
        return response

    async def gate_logout(request):
        sid = request.cookies.get(GATE_COOKIE, "")
        if request.method == "GET":
            # Signing out changes state, so it takes a POST carrying this page's token.
            return page(
                "Sign out",
                '<form method="post" action="/_vecten/gate/logout">'
                f"{hidden('csrf', gate.logout_token(sid))}<button>Sign out</button></form>",
            )
        form = await request.form()
        gate.logout(sid, form.get("csrf", ""))
        response = page(
            "Signed out", '<p>You are signed out of this site.</p><p><a href="/">Sign in</a></p>'
        )
        gate_cookie_cleared(response, GATE_COOKIE)
        return response

    async def gate_verify(request):
        try:
            status, headers = gate.verify(
                request.cookies.get(GATE_COOKIE, ""),
                request.headers.get(HOST_HEADER, ""),
                request.headers.get(ROUTE_HEADER, ""),
            )
        except Exception as exc:
            # NGINX answers 500 for any status but 2xx, 401 and 403, so nobody is admitted.
            logger.error("gate verify failed: %s", describe_error(exc))
            status, headers = 500, {}
        return Response(status_code=status, headers={**headers, "Cache-Control": "no-store"})

    async def connected(request):
        principal = current_browser(request)
        csrf = secrets.token_urlsafe(32)
        clients = {
            s["client_id"]
            for _, s in state.scan("sessions")
            if s["principal_id"] == principal["id"] and s["expires_at"] > service.clock()
        }
        body = "".join(
            f'<form method="post" action="/connected-clients" class="client">'
            f"{hidden('client_id', cid)}{hidden('csrf', csrf)}"
            f"<span>{escape(service.client(cid)['client_name'])}</span>"
            '<button class="secondary">Revoke</button></form>'
            for cid in clients
        )
        response = page("Connected clients", body or "<p>No connected clients.</p>")
        response.set_cookie(
            "vecten_clients_csrf",
            csrf,
            secure=secure(request),
            httponly=True,
            samesite="strict",
            max_age=600,
        )
        return response

    async def connected_revoke(request):
        principal = current_browser(request)
        form = await request.form()
        cookie = request.cookies.get("vecten_clients_csrf", "")
        if not cookie or not secrets.compare_digest(cookie, str(form.get("csrf", ""))):
            raise ValueError("invalid browser state")
        service.revoke_client(principal["id"], str(form.get("client_id", "")))
        return RedirectResponse("/connected-clients", status_code=303)

    async def revoke(request):
        form = await request.form()
        token, cid = str(form.get("token", "")), str(form.get("client_id", ""))
        row = state.get("access", digest(token), now=service.clock()) or state.get(
            "refresh", digest(token), now=service.clock()
        )
        session = state.get("sessions", row["sid"]) if row else None
        if session and session["client_id"] == cid:
            service.revoke(row["sid"])
        return JSONResponse({})

    async def password_setup(request):
        if not hasattr(service.backend, "set_password"):
            raise ValueError("password backend is not selected")
        if request.method == "GET":
            csrf = secrets.token_urlsafe(32)
            body = (
                f'<form method="post">{hidden("csrf", csrf)}'
                '<label>Setup token <input name="token" type="password" required></label>'
                '<label>New password <input name="password" type="password" '
                'autocomplete="new-password" minlength="12" required></label>'
                "<button>Set password</button></form>"
            )
            response = page("Set your password", body)
            response.set_cookie(
                "vecten_setup_csrf",
                csrf,
                secure=secure(request),
                httponly=True,
                samesite="strict",
                max_age=600,
            )
            return response
        form = await request.form()
        cookie = request.cookies.get("vecten_setup_csrf", "")
        if not cookie or not secrets.compare_digest(cookie, str(form.get("csrf", ""))):
            raise ValueError("invalid browser state")
        principal_id = service.backend.set_password(
            str(form.get("token", "")), str(form.get("password", "")), address(request)
        )
        service.revoke_principal(principal_id)
        return page("Password set", "<p>Return to your client to sign in.</p>")

    async def jwks(request):
        return JSONResponse(service.keys.jwks())

    def safe(handler, browser=False):
        async def wrapped(request):
            try:
                if int(request.headers.get("content-length", "0")) > 16384:
                    return JSONResponse({"error": "invalid_request"}, status_code=413)
                response = await handler(request)
            except RegistrationError as exc:
                response = JSONResponse(
                    {"error": exc.error, "error_description": exc.description}, status_code=400
                )
            except RateLimit:
                response = (
                    failure_page("temporarily_unavailable", 429)
                    if browser
                    else JSONResponse({"error": "temporarily_unavailable"}, status_code=429)
                )
                response.headers["Retry-After"] = "60"
            except ValueError, KeyError, TypeError:
                # OAuth polling needs its defined error code, never raw library errors.
                import sys

                error = str(sys.exception())
                allowed = {
                    "authorization_pending",
                    "slow_down",
                    "access_denied",
                    "expired_token",
                    "invalid_client",
                    "invalid_grant",
                    "invalid_target",
                    "not_provisioned",
                    "unsupported_grant_type",
                    "unsupported_response_type",
                }
                code = error if error in allowed else "invalid_request"
                response = (
                    failure_page(code, 400)
                    if browser
                    else JSONResponse({"error": code}, status_code=400)
                )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return response

        return wrapped

    endpoints = [
        ("/.well-known/oauth-authorization-server", metadata, ["GET"]),
        ("/.well-known/oauth-protected-resource/mcp", resource, ["GET"]),
        ("/.well-known/oauth-protected-resource", resource, ["GET"]),
        ("/register", register, ["POST"]),
        ("/authorize", authorize, ["GET"]),
        ("/login", login, ["POST"]),
        ("/consent", consent, ["POST"]),
        ("/token", token, ["POST"]),
        ("/device", device, ["POST"]),
        ("/device/verify", verify_device, ["GET"]),
        ("/oidc/start", oidc_start, ["POST"]),
        ("/oidc/callback", oidc_callback, ["GET"]),
        ("/connected-clients", connected, ["GET"]),
        ("/connected-clients", connected_revoke, ["POST"]),
        ("/revoke", revoke, ["POST"]),
        ("/set-password", password_setup, ["GET", "POST"]),
        ("/jwks", jwks, ["GET"]),
    ]
    if gate is not None:
        endpoints += [
            ("/gate/start", gate_start, ["GET"]),
            ("/gate/authorize", gate_authorize, ["GET"]),
            ("/gate/callback", gate_callback, ["GET"]),
            ("/gate/logout", gate_logout, ["GET", "POST"]),
        ]
    routes = [
        Route(path, safe(handler, browser=path in BROWSER), methods=methods)
        for path, handler, methods in endpoints
    ]
    if gate is not None:
        # NGINX asks on every request of a login route, all from one address per host, so no
        # per-address limit applies; an unknown session gets 401 and nothing else.
        routes.append(Route("/gate/verify", gate_verify, methods=["GET"]))
    return routes
