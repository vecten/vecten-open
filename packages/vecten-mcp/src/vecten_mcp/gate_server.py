"""Service-host login endpoints using the shared state database."""

import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from vecten_mcp.apps import AppScopes, read_bound_apps
from vecten_mcp.assets import page_asset_route
from vecten_mcp.auth.gate import GateService
from vecten_mcp.auth.routes import auth_routes
from vecten_mcp.http_limits import BodyLimit
from vecten_mcp.runtime_setup import state_url
from vecten_mcp.state import StateStore
from vecten_shared.config import load_config
from vecten_shared.extension_record import read_record
from vecten_shared.provider import get_provider


@dataclass(frozen=True)
class GateIssuer:
    issuer: str
    state: StateStore

    clock = staticmethod(time.time)


def configured_gate():
    root = Path(os.environ.get("VECTEN_PROJECT_ROOT", "/app"))
    config = load_config(root)
    env = os.environ["VECTEN_ENV"]
    cloud = os.environ["VECTEN_PROVIDER"]
    spec = get_provider(cloud, root, env).connection(env, "mcp_state")
    state = StateStore(
        state_url(spec, os.environ["MCP_GATE_DB_PASSWORD"], cloud).set(username="mcp_gate"),
        initialize=False,
    )
    issuer = GateIssuer(os.environ["VECTEN_MCP_PUBLIC_URL"], state)
    apps = AppScopes(state, read_bound_apps(root, config), kind=config.warehouse.kind)
    return GateService.from_config(issuer, config, apps=apps, record=read_record(root) or {})


def create_app(factory=configured_gate):
    @asynccontextmanager
    async def lifespan(app):
        gate = factory()
        app.router.routes.extend(auth_routes(gate.oauth, gate, surface="service"))
        try:
            yield
        finally:
            gate.state.close()

    async def health(request):
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/health", health), page_asset_route()], lifespan=lifespan)
    app.add_middleware(BodyLimit)
    return app


if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8002, proxy_headers=False)
