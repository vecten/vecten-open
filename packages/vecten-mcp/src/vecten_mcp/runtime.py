"""One process owns authentication, warehouse sessions and periodic maintenance."""

import asyncio
import logging
from dataclasses import dataclass, field

from vecten_mcp.failures import describe_error

logger = logging.getLogger(__name__)


@dataclass
class Runtime:
    state: object
    oauth: object
    extensions: object
    adapter: object
    pools: object
    administration: object = None
    access: object = None
    password_rotation: object = None
    model: object = None
    audit: object = None
    queries: object = None
    feedback_resolution: object = None
    apps: object = None
    maintenance_task: object = field(default=None, init=False)
    last_reconcile: float = field(default=0, init=False)
    last_model_check: float = field(default=0, init=False)
    last_audit_maintenance: float = field(default=0, init=False)

    tool_service: object = field(default=None, init=False)
    admin_service: object = field(default=None, init=False)
    feedback: object = field(default=None, init=False)

    def __post_init__(self):
        from vecten_mcp.admin_service import AdminService
        from vecten_mcp.feedback import FeedbackService
        from vecten_mcp.tool_service import ToolService

        self.tool_service = ToolService(self)
        self.admin_service = AdminService(self)
        self.feedback = FeedbackService(self)

    def start(self):
        if self.adapter:
            registry = dict(self.state.scan("provisioner_registry"))
            self.adapter.check_scope(registry)
            self.adapter.cleanup_sessions()
        if self.model:
            self.model.reload()
        if self.apps and self.access:
            # A ship restarts the server with the new bundle, so admission follows its binding
            # from the first request, before the first reconciliation.
            self.access.admit()
        if self.feedback_resolution:
            try:
                self.feedback_resolution.reconcile()
            except Exception as exc:
                logger.error("MCP feedback reconciliation failed: %s", describe_error(exc))
        if self.audit:
            self.audit.start()
        self.maintenance_task = asyncio.create_task(self.maintenance())

    async def maintenance(self):
        while True:
            await asyncio.sleep(30)
            try:
                await asyncio.to_thread(self.tick)
            except Exception as exc:
                logger.error("MCP maintenance failed: %s", describe_error(exc))

    def tick(self):
        # Each maintenance concern retries independently; an unavailable warehouse must
        # not prevent token cleanup or signing-key rotation.
        def attempt(operation):
            try:
                operation()
                return True
            except Exception as exc:
                logger.error("MCP maintenance failed: %s", describe_error(exc))
                return False

        attempt(self.oauth.maintain)
        attempt(self.feedback.fold)
        if self.pools:
            attempt(self.pools.expire)
        now = self.oauth.clock()
        if self.audit and now - self.last_audit_maintenance >= 86400:

            def retain():
                setting = self.state.get("settings", "audit_retention_days")
                self.state.maintain_audit(setting["value"] if setting else 90)

            if attempt(retain):
                self.last_audit_maintenance = now
        if self.model and now - self.last_model_check >= 180:
            if attempt(self.model.reload):
                self.last_model_check = now
        if self.feedback_resolution:
            attempt(self.feedback_resolution.reconcile)
        if self.access and now - self.last_reconcile >= 900:
            if attempt(self.access.reconcile):
                self.last_reconcile = now
        if self.password_rotation:
            attempt(self.password_rotation.tick)
        attempt(self.oauth.keys.maintain)

    async def close(self):
        if self.maintenance_task:
            self.maintenance_task.cancel()
            try:
                await self.maintenance_task
            except asyncio.CancelledError:
                pass
        if self.pools:
            await asyncio.to_thread(self.pools.close)
        if self.audit:
            await asyncio.to_thread(self.audit.close)
        backend = self.oauth.backend
        if hasattr(backend, "client"):
            backend.client.close()
        self.state.close()


def build_runtime(root, config, extensions):
    import os

    from vecten_mcp.runtime_setup import configured_runtime
    from vecten_shared.declarations import check_settings
    from vecten_shared.extension_record import read_record

    # The server loads every extension now, so it checks their settings now, before /health
    # can answer: a tool must never run on settings its manifest refuses.
    check_settings(config, read_record(root) or {}, os.environ.get("VECTEN_ENV", "local"))
    return configured_runtime(root, config, extensions)
