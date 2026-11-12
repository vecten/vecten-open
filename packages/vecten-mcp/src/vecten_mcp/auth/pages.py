"""Small escaped forms; all state-changing submissions carry a browser-bound nonce."""

from html import escape

from starlette.responses import HTMLResponse

from vecten_mcp.assets import LOGO, STYLESHEET


def page(title, body, status=200, *, head=""):
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>{STYLESHEET}{head}</head>"
        f'<body class="signin"><main>{LOGO}<p class="eyebrow">Vecten Open</p>'
        f"<h1>{escape(title)}</h1>{body}</main></body></html>",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'self'; font-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def redirect_page(target):
    # CSP form-action applies to HTTP redirects after a POST. Navigate from a new
    # document instead; callers supply the validated OAuth or OIDC destination.
    target = escape(target, quote=True)
    return page(
        "Continue signing in",
        f'<p><a href="{target}">Continue</a></p>',
        head=f'<meta http-equiv="refresh" content="0;url={target}">',
    )


def hidden(name, value):
    return (
        f'<input type="hidden" name="{escape(name, quote=True)}" '
        f'value="{escape(str(value), quote=True)}">'
    )


def login_page(flow, csrf, *, oidc=False, error=None):
    fields = hidden("flow", flow) + hidden("csrf", csrf)
    if oidc:
        body = (
            f'<form method="post" action="/oidc/start">{fields}'
            "<button>Sign in with your provider</button></form>"
        )
    else:
        body = (
            f'<form method="post" action="/login">{fields}'
            '<label>Email <input name="email" type="email" '
            'autocomplete="username" required></label>'
            '<label>Password <input name="password" type="password" '
            'autocomplete="current-password" required></label>'
            "<button>Sign in</button></form>"
        )
    if error:
        body = f'<p class="error">{escape(error)}</p>' + body
    return page("Sign in to Vecten", body, 400 if error else 200)


def consent_page(flow, csrf, client):
    return page(
        "Allow this client?",
        f"<p>{escape(client)} will act as you with your warehouse permissions.</p>"
        '<form method="post" action="/consent">'
        f"{hidden('flow', flow)}{hidden('csrf', csrf)}"
        '<button name="decision" value="approve">Approve</button>'
        '<button name="decision" value="deny" class="secondary">Deny</button></form>',
    )


FAILURES = {
    "access_denied": "The sign-in was declined, so nothing was shared with the client.",
    "temporarily_unavailable": "There were too many attempts from here. Wait a minute.",
    "not_provisioned": "Your account has no access yet. Ask an admin to add you, then try again.",
}
# Expired, already used or tampered with: the person cannot tell these apart, nor needs to.
EXPIRED = "This sign-in can't continue: its link expired, was already used, or sat open too long."


def failure_page(code, status):
    reason = FAILURES.get(code, EXPIRED)
    return page(
        "Sign-in stopped",
        f'<p class="error">{escape(reason)}</p>'
        "<p>Close this tab and start again from your client.</p>",
        status,
    )
