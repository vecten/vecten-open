"""`/apps`: the analytics apps a signed-in person may open, with links, and nothing about any
other app."""

from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse
from starlette.routing import Route

from vecten_mcp.apps import app_url
from vecten_mcp.panel.html import form, join, layout, link, tag
from vecten_mcp.panel.session import PanelSessions

APPS_CLIENT = "vecten-apps-page"
# The __Host- prefix requires Path=/, which would send the cookie to /mcp and /admin too; with
# Path=/apps a sibling hostname can plant one, which signs the browser in as the planter only.
SESSION_COOKIE = "vecten_apps"
LOGIN_COOKIE = "vecten_apps_login"
BRAND = "Vecten"
FORM_LIMIT = 4096


def openable(runtime, groups):
    """The bound apps with a link that admit one of `groups`."""
    apps = getattr(runtime, "apps", None)
    if apps is None:
        return []
    found = []
    for app in sorted(apps.apps.values(), key=lambda a: (a.title, a.key)):
        url = app_url(runtime.oauth.issuer, app.hostname) if app.kind == "container" else None
        if url and apps.admitted(app.name) & groups:
            found.append((app, url))
    return found


def page(title, body, *, status=200):
    return layout(title, body, status=status, nav=False, brand=BRAND)


def people_routes(runtime):
    sessions = PanelSessions(
        runtime.oauth,
        base="/apps",
        client_id=APPS_CLIENT,
        client_name="Vecten apps page",
        cookie=SESSION_COOKIE,
        login_cookie=LOGIN_COOKIE,
        kind="apps_page",
    )

    async def listing(request):
        current = sessions.current(request)
        if current is None:
            return RedirectResponse("/apps/login", status_code=303)
        caller, record = current
        found = await run_in_threadpool(openable, runtime, set(caller.groups))
        if found:
            items = join(
                *(
                    tag("li", join(link(url, app.title), " ", tag("span", app.description)))
                    for app, url in found
                )
            )
            body = tag("ul", items)
        else:
            body = tag(
                "p",
                "No analytics app is open to your groups. An administrator decides who may "
                "open each app.",
                class_="empty",
            )
        signed = tag("p", f"Signed in as {caller.principal['email']}.", class_="hint")
        logout = form("/apps/logout", record["csrf"], button="Sign out", class_="inline")
        return page("Analytics apps", join(signed, body, logout))

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
            return page(
                "Sign-in failed",
                tag("p", "The sign-in expired or was started in another window. Start again."),
                status=400,
            )
        token, max_age = sessions.open(caller)
        runtime.oauth.event(caller.principal["id"], "apps/login", "", "allowed")
        response = RedirectResponse("/apps", status_code=303)
        sessions.cookie(response, SESSION_COOKIE, token, max_age, origin)
        response.delete_cookie(LOGIN_COOKIE, path="/apps")
        return response

    async def logout(request):
        current = sessions.current(request)
        if current is None:
            return page("Signed out", tag("p", "No session is open."))
        if int(request.headers.get("content-length", "0")) > FORM_LIMIT:
            return page("Request too large", tag("p", "The form is too large."), status=413)
        caller, record = current
        if not sessions.csrf_ok(record, dict(await request.form())):
            return page(
                "Request refused", tag("p", "The form expired; reload the page."), status=403
            )
        sessions.close(record)
        runtime.oauth.event(caller.principal["id"], "apps/logout", "", "allowed")
        response = page("Signed out", tag("p", link("/apps", "Sign in again")))
        response.delete_cookie(SESSION_COOKIE, path="/apps")
        return response

    return [
        Route("/apps", listing, methods=["GET"]),
        Route("/apps/login", login, methods=["GET"]),
        Route("/apps/callback", callback, methods=["GET"]),
        Route("/apps/logout", logout, methods=["POST"]),
    ]


__all__ = ["APPS_CLIENT", "people_routes"]
