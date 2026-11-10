"""The Vecten design system's stylesheet, fonts and logo, served by the MCP itself.

The browser pages' CSP admits styles and fonts from this origin only, so nothing here comes
from a CDN. The assets live outside /admin because the sign-in pages need them even where the
panel is off or limited to some addresses.
"""

from importlib import resources

from starlette.responses import Response
from starlette.routing import Route

ASSETS = {
    "vecten.css": "text/css; charset=utf-8",
    "fonts/Geist-Variable.ttf": "font/ttf",
    "fonts/GeistMono-Variable.ttf": "font/ttf",
    "fonts/OFL.txt": "text/plain; charset=utf-8",
}
STYLESHEET = '<link rel="stylesheet" href="/assets/vecten.css">'
# Inline, so it needs no img-src and takes the text colour of the surface it sits on.
LOGO = resources.files(__name__).joinpath("static/logo.svg").read_text().strip()


async def _serve(request):
    name = request.path_params["name"]
    media_type = ASSETS.get(name)
    if media_type is None:
        return Response(status_code=404)
    body = resources.files(__name__).joinpath("static", name).read_bytes()
    return Response(
        body,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


def asset_routes():
    return [Route("/assets/{name:path}", _serve, methods=["GET"])]
