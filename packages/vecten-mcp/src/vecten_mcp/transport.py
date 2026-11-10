"""SDK transport with authorization repeated at both discovery and invocation."""

import asyncio
import logging
from contextvars import ContextVar
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent

from vecten_mcp import __version__
from vecten_mcp.audit_call import AuditCall, current_audit
from vecten_mcp.auth.service import is_admin
from vecten_mcp.failures import describe_error, sign_in_reason, warehouse_reason

current_request = ContextVar("vecten_request", default=None)
logger = logging.getLogger(__name__)


class Verifier:
    def __init__(self, runtime):
        self.runtime = runtime

    async def verify_token(self, token):
        try:
            resolved = self.runtime.oauth.resolve(token)
            return AccessToken(
                token=token,
                client_id=resolved.client_id,
                scopes=["mcp"],
                expires_at=int(resolved.expires_at),
                resource=self.runtime.oauth.resource,
                subject=resolved.principal["id"],
            )
        except ValueError:
            return None


class AuthorizedMCP(MCPServer):
    def __init__(self, runtime):
        self.runtime = runtime
        super().__init__(
            "Vecten",
            version=__version__,
            token_verifier=Verifier(runtime),
            auth=AuthSettings(
                issuer_url=runtime.oauth.issuer,
                resource_server_url=runtime.oauth.resource,
                validate_token_resource=True,
                required_scopes=["mcp"],
            ),
        )
        for name, tool in runtime.extensions.tools.items():
            self.add_tool(tool["handler"], name=name)

    def resolved(self):
        access = get_access_token()
        if access is None:
            raise ValueError("authentication required")
        return self.runtime.oauth.resolve(access.token)

    def allowed_tools(self, resolved):
        return self.runtime.extensions.tools_for(
            resolved.groups,
            admin=is_admin(resolved),
        )

    async def list_tools(self):
        resolved = self.resolved()
        allowed = self.allowed_tools(resolved)
        return [tool for tool in await super().list_tools() if tool.name in allowed]

    async def call_tool(self, name, arguments, context=None):
        resolved = self.resolved()
        call = AuditCall(self.runtime, resolved, name, arguments) if self.runtime.audit else None
        token = current_request.set((self.runtime, resolved))
        audit_token = current_audit.set(call)
        result, error = None, None
        try:
            if call:
                call.prepare(await asyncio.to_thread(call.policy, self.runtime))
            if name not in self.allowed_tools(resolved):
                if call:
                    call.refuse("tool_not_available")
                result = CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text="Tool not available for your groups. "
                            "Use vecten user and vecten grant for administration.",
                        )
                    ],
                    is_error=True,
                )
            else:
                result = await super().call_tool(name, arguments, context)
            if self.runtime.feedback_resolution:
                try:
                    notices = await asyncio.to_thread(
                        self.runtime.feedback_resolution.notices, resolved
                    )
                except Exception as exc:
                    logger.error("MCP definition notice failed: %s", describe_error(exc))
                    notices = []
                if notices:
                    result.meta = {**(result.meta or {}), "vecten/definition_changes": notices}
                    result.content.append(
                        TextContent(
                            type="text",
                            text="Definitions you reported have changed: "
                            + "; ".join(", ".join(n["definitions"]) or n["gap"] for n in notices),
                        )
                    )
            return result
        except asyncio.CancelledError as exc:
            error = exc
            raise
        except Exception as exc:
            error = exc
            receipt = call.receipt["id"] if call else None
            if isinstance(exc, ToolError) and not isinstance(exc, UnexpectedToolError):
                # Failed argument validation and failures a tool anticipated are text for the
                # caller; the log keeps only the class, since the arguments are the caller's.
                cause = type(exc.__cause__ or exc).__name__
                logger.info("MCP tool %s refused, receipt %s (%s)", name, receipt, cause)
                text = str(exc)
            else:
                logger.error(
                    "MCP tool %s failed, receipt %s: %s", name, receipt, describe_error(exc)
                )
                text = warehouse_reason(exc) or sign_in_reason(exc) or "Tool execution failed"
            result = CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
            return result
        finally:
            try:
                if call:
                    call.finish(result, error)
            finally:
                current_audit.reset(audit_token)
                current_request.reset(token)


def transport_app(runtime):
    server = AuthorizedMCP(runtime)
    hostname = urlparse(runtime.oauth.issuer).netloc
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=65536,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[hostname], allowed_origins=[runtime.oauth.issuer]
        ),
    )
    return server, app
