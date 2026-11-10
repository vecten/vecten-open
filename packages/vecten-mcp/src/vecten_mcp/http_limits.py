"""Bound HTTP request bodies before forms, JSON or SDK handlers allocate them."""

from starlette.responses import JSONResponse


class BodyLimit:
    def __init__(self, app, limit=65536):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > self.limit:
                response = JSONResponse({"error": "request too large"}, status_code=413)
                return await response(scope, receive, send)
            if body:
                chunks.append(body)
            if not message.get("more_body", False):
                break
        pending = True

        async def bounded_receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, bounded_receive, send)
