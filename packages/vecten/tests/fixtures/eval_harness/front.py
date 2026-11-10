"""One TLS listener for every stub hostname, routing each request by its Host header.

The stub URLs carry no port, so every hostname shares 443 on the loopback address; one
certificate names them all. An application that needs its lifespan (the MCP) is held open by
a task on the server's own loop, because its task groups must exit in the task that entered
them.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future
from pathlib import Path

import uvicorn


async def _plain(send, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class Front:
    def __init__(self):
        self.apps: dict[str, object] = {}
        self.waiting: dict[str, str] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._held: dict[str, tuple[asyncio.Event, Future]] = {}

    def mount(self, host: str, app) -> None:
        self.apps[host] = app
        self.waiting.pop(host, None)

    def wait(self, host: str, reason: str) -> None:
        """Answer `host` with 503 and `reason` until an application is mounted for it."""
        self.waiting[host] = reason

    def mount_with_lifespan(self, host: str, app, timeout: float = 120) -> None:
        """Run `app`'s lifespan on the server loop, then route `host` to it, replacing and
        closing any application held for `host` before. Called from another thread."""
        if self.loop is None:
            raise RuntimeError("the front server is not running")
        started = Future()
        stop = asyncio.Event()

        async def hold():
            async with app.router.lifespan_context(app):
                started.set_result(True)
                await stop.wait()

        done = asyncio.run_coroutine_threadsafe(hold(), self.loop)
        try:
            started.result(timeout)
        except Exception:
            done.cancel()
            raise
        previous = self._held.pop(host, None)
        self.mount(host, app)
        self._held[host] = (stop, done)
        if previous is not None:
            self.loop.call_soon_threadsafe(previous[0].set)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    self.loop = asyncio.get_running_loop()
                    await send({"type": "lifespan.startup.complete"})
                else:
                    for stop, _ in self._held.values():
                        stop.set()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"").decode("latin-1").rsplit(":", 1)[0].lower()
        if host.startswith("[") or host.count(":") > 1:
            host = ""
        app = self.apps.get(host)
        if app is not None:
            return await app(scope, receive, send)
        if scope["type"] != "http":
            return
        if host in self.waiting:
            return await _plain(send, 503, self.waiting[host])
        return await _plain(send, 421, f"the eval harness serves no host {host!r}")

    def server(self, host: str, port: int, cert: Path, key: Path) -> uvicorn.Server:
        config = uvicorn.Config(
            self,
            host=host,
            port=port,
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
            lifespan="on",
            log_level="warning",
            proxy_headers=False,
            # Clients hold keep-alive connections open, which would stall a shutdown.
            timeout_graceful_shutdown=2,
        )
        return uvicorn.Server(config)
