"""Panel routes under /admin: sign-in, the admin guard, CSRF and the screens."""

from dataclasses import dataclass

from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse
from starlette.routing import Route

from vecten_mcp.admin_service import AdminRefused, AdminService, OperationRefused
from vecten_mcp.panel import screens
from vecten_mcp.panel.html import layout, link, refused, tag
from vecten_mcp.panel.session import LOGIN_COOKIE, SESSION_COOKIE, PanelSessions

FORM_LIMIT = 65536


@dataclass
class Context:
    request: object
    form: dict | None
    caller: object
    csrf: str
    service: AdminService

    def call(self, operation, data=None, *, write=False):
        return self.service.call(self.caller, operation, data or {}, surface="panel", write=write)

    def query(self, name, default=None):
        return self.request.query_params.get(name, default)

    def param(self, name):
        return self.request.path_params[name]


def panel_routes(runtime):
    service = getattr(runtime, "admin_service", None) or AdminService(runtime)
    sessions = PanelSessions(runtime.oauth)

    def guarded(handler):
        async def endpoint(request):
            current = sessions.current(request)
            if current is None:
                if request.method == "GET":
                    return RedirectResponse("/admin/login", status_code=303)
                return refused("Sign in required", "Sign in to the admin panel first.", 403)
            caller, record = current
            try:
                service.authorize(caller)
            except AdminRefused:
                return refused(
                    "Admin access required", "This account does not hold the admin flag.", 403
                )
            form = None
            if request.method == "POST":
                if int(request.headers.get("content-length", "0")) > FORM_LIMIT:
                    return refused("Request too large", "The form is too large.", 413)
                form = dict(await request.form())
                if not sessions.csrf_ok(record, form):
                    return refused("Request refused", "The form expired; reload the page.", 403)
            context = Context(request, form, caller, record["csrf"], service)
            try:
                return await run_in_threadpool(handler, context)
            except AdminRefused, PermissionError:
                return refused("Admin access required", "The admin flag was removed.", 403)
            except OperationRefused as exc:
                return refused("Request refused", str(exc), exc.status, csrf=record["csrf"])
            except (ValueError, KeyError, TypeError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "The request is incomplete."
                return refused("Request refused", message, 400, csrf=record["csrf"])

        return endpoint

    async def login(request):
        target, nonce, origin = sessions.begin(request)
        response = RedirectResponse(target, status_code=303)
        sessions.cookie(response, LOGIN_COOKIE, nonce, 600, origin)
        return response

    async def callback(request):
        origin = sessions.origin(request)
        try:
            caller = await run_in_threadpool(sessions.finish, request)
        except ValueError:
            return refused(
                "Sign-in failed",
                "The sign-in expired or was started in another window. Start again.",
                400,
            )
        try:
            service.authorize(caller)
        except AdminRefused:
            runtime.oauth.revoke(caller.session_id)
            runtime.oauth.event(caller.principal["id"], "panel/login", "", "denied")
            return refused(
                "Admin access required", "This account does not hold the admin flag.", 403
            )
        token, max_age = sessions.open(caller)
        runtime.oauth.event(caller.principal["id"], "panel/login", "", "allowed")
        response = RedirectResponse("/admin", status_code=303)
        sessions.cookie(response, SESSION_COOKIE, token, max_age, origin)
        response.delete_cookie(LOGIN_COOKIE, path="/admin")
        return response

    async def logout(request):
        current = sessions.current(request)
        if current is None:
            return refused("Signed out", "No panel session is open.", 200)
        caller, record = current
        form = dict(await request.form())
        if not sessions.csrf_ok(record, form):
            return refused("Request refused", "The form expired; reload the page.", 403)
        sessions.close(record)
        runtime.oauth.event(caller.principal["id"], "panel/logout", "", "allowed")
        response = layout("Signed out", tag("p", link("/admin/login", "Sign in again")), nav=False)
        response.delete_cookie(SESSION_COOKIE, path="/admin")
        return response

    routes = [
        Route("/admin/login", login, methods=["GET"]),
        Route("/admin/callback", callback, methods=["GET"]),
        Route("/admin/logout", logout, methods=["POST"]),
    ]
    routes += [
        Route(path, guarded(handler), methods=methods) for path, handler, methods in screens.SCREENS
    ]
    return routes


__all__ = ["Context", "panel_routes"]
