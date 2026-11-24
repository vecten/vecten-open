"""Same-origin browser assets for the MCP and its service-host login gate."""

from importlib import resources

from starlette.responses import Response
from starlette.routing import Route

from vecten_shared.page_assets import PAGE_ASSET_PATH, PAGE_ASSET_TYPES

_STATIC = resources.files(__name__).joinpath("static")
_ASSET_BYTES = {name: _STATIC.joinpath(name).read_bytes() for name in PAGE_ASSET_TYPES}
PAGE_ASSETS = (
    f'<link rel="stylesheet" href="{PAGE_ASSET_PATH}/vecten.css">'
    f'<link rel="icon" type="image/png" href="{PAGE_ASSET_PATH}/favicon.png">'
)
ASSET_CSP = "style-src 'self'; font-src 'self'; img-src 'self';"
# Inline so the logo takes the text colour of its surface.
LOGO = _STATIC.joinpath("logo.svg").read_text().strip()


def _asset(name):
    if name not in _ASSET_BYTES:
        return Response(status_code=404)
    return Response(
        _ASSET_BYTES[name],
        media_type=PAGE_ASSET_TYPES[name],
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


async def _serve(request):
    return _asset(request.path_params["name"])


async def _favicon(request):
    return _asset("favicon.png")


def page_asset_route():
    return Route(f"{PAGE_ASSET_PATH}/{{name:path}}", _serve, methods=["GET"])


def asset_routes():
    return [
        page_asset_route(),
        Route("/assets/{name:path}", _serve, methods=["GET"]),
        Route("/favicon.ico", _favicon, methods=["GET"]),
    ]
