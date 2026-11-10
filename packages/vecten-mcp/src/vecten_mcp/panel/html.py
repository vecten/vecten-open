"""Escaped HTML builders: every value is escaped unless it is already `Html`."""

from html import escape
from urllib.parse import urlencode

from starlette.responses import HTMLResponse, RedirectResponse

from vecten_mcp.assets import LOGO, STYLESHEET

HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; font-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
NAV = (
    ("/admin", "Home"),
    ("/admin/users", "Users and groups"),
    ("/admin/access", "Grants and deny lists"),
    ("/admin/model", "Model"),
    ("/admin/analytics", "Usage"),
    ("/admin/gaps", "Gaps"),
    ("/admin/audit", "Audit"),
    ("/admin/clients", "Connected clients"),
    ("/admin/settings", "Settings"),
)


class Html(str):
    pass


def esc(value):
    if isinstance(value, Html):
        return value
    if value is None:
        return Html("")
    if isinstance(value, bool):
        return Html("yes" if value else "no")
    return Html(escape(str(value), quote=True))


def join(*parts):
    return Html("".join(esc(part) for part in parts))


def tag(element, content="", /, **attributes):
    attrs = "".join(
        f' {key.rstrip("_").replace("_", "-")}="{esc(value)}"'
        for key, value in attributes.items()
        if value is not None and value is not False
    )
    return Html(f"<{element}{attrs}>{esc(content)}</{element}>")


def link(href, text):
    return tag("a", text, href=href)


def table(headers, rows, *, empty="Nothing to show."):
    if not rows:
        return tag("p", empty, class_="empty")
    head = tag("tr", join(*(tag("th", h) for h in headers)))
    body = join(*(tag("tr", join(*(tag("td", cell) for cell in row))) for row in rows))
    return tag("table", join(tag("thead", head), tag("tbody", body)))


def hidden(name, value):
    return Html(f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">')


def field(label, name, *, value="", type_="text", required=True, **extra):
    attributes = {"name": name, "type": type_, "value": value, "required": required, **extra}
    attrs = "".join(
        f' {key.rstrip("_").replace("_", "-")}="{esc(v)}"' if v is not True else f" {key}"
        for key, v in attributes.items()
        if v is not None and v is not False
    )
    return Html(f"<label>{esc(label)} <input{attrs}></label>")


def form(action, csrf, *parts, button="Save", method="post", class_=None):
    return Html(
        f'<form method="{esc(method)}" action="{esc(action)}"'
        + (f' class="{esc(class_)}"' if class_ else "")
        + ">"
        + (hidden("csrf", csrf) if method == "post" else "")
        + join(*parts)
        + f"<button>{esc(button)}</button></form>"
    )


def layout(title, body, *, csrf=None, status=200, nav=True, notice=None):
    menu = ""
    if nav:
        items = join(*(tag("li", link(href, text)) for href, text in NAV))
        logout = form("/admin/logout", csrf, button="Sign out", class_="inline") if csrf else ""
        menu = tag("nav", join(tag("ul", items), logout))
    message = tag("p", notice, class_="notice") if notice else ""
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} · Vecten admin</title>{STYLESHEET}</head>"
        f'<body class="panel"><header><p class="brand">{LOGO}Vecten admin</p>{menu}</header>'
        f"<main><h1>{esc(title)}</h1>{message}{esc(body)}</main></body></html>"
    )
    return HTMLResponse(page, status_code=status, headers=HEADERS)


def refused(title, message, status, *, csrf=None):
    return layout(title, tag("p", message), status=status, csrf=csrf)


def done(path, operation):
    return RedirectResponse(path + "?" + urlencode({"done": operation}), status_code=303)
