"""Compose health, OAuth, admin API and the SDK's authenticated streamable transport."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from vecten_shared.versions import VERSIONS


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": VERSIONS.vecten})


def create_app(project_root: Path | None = None, runtime=None) -> Starlette:
    from vecten_mcp.extensions import McpExtensions
    from vecten_shared.config import load_config

    root = project_root or Path(os.environ.get("VECTEN_PROJECT_ROOT", "/app"))
    config = load_config(root) if (root / "vecten.toml").is_file() else None
    extensions = McpExtensions.from_config(config) if config else None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal runtime
        if runtime is None and config:
            from vecten_mcp.runtime import build_runtime

            runtime = build_runtime(root, config, extensions)
        if runtime is None:
            yield
            return
        from vecten_mcp.auth.routes import auth_routes
        from vecten_mcp.transport import transport_app

        server, transport = transport_app(runtime)
        app.state.runtime = runtime
        app.state.mcp = server
        runtime.start()
        from vecten_mcp.assets import asset_routes

        app.router.routes.extend(asset_routes())
        app.router.routes.extend(auth_routes(runtime.oauth))
        if runtime.administration:
            from vecten_mcp.admin_routes import admin_routes
            from vecten_mcp.panel.routes import panel_routes

            app.router.routes.extend(admin_routes(runtime))
            app.router.routes.extend(panel_routes(runtime))
        app.router.routes.append(Mount("/", app=transport))
        try:
            async with transport.router.lifespan_context(transport):
                yield
        finally:
            await runtime.close()

    from vecten_mcp.http_limits import BodyLimit

    app = Starlette(routes=[Route("/health", health)], lifespan=lifespan)
    app.add_middleware(BodyLimit)
    if extensions is not None:
        app.state.extensions = extensions
    return app


app = create_app()
