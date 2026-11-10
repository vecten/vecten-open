"""Installed MCP hooks; callers supply authenticated groups and persist audit rows first."""

from dataclasses import dataclass, field

from vecten_shared.config import VectenConfig
from vecten_shared.extensions import load_group, selected_entry_points
from vecten_shared.output import ExitCode, Failure


@dataclass
class McpExtensions:
    tools: dict
    auth: dict
    provisioning: dict
    audit_sinks: dict
    tool_plugins: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: VectenConfig) -> McpExtensions:
        groups = [
            load_group(config, f"vecten.mcp.{name}")
            for name in ("tools", "auth", "provisioning", "audit_sinks")
        ]
        instance = cls(*groups)
        instance.tool_plugins = {
            point.name: point.dist.name
            for point in selected_entry_points(config, "vecten.mcp.tools")
        }
        for name, tool in instance.tools.items():
            if (
                not isinstance(tool, dict)
                or not callable(tool.get("handler"))
                or not isinstance(tool.get("groups"), list)
                or not all(isinstance(g, str) and g for g in tool["groups"])
                or type(tool.get("admin", False)) is not bool
            ):
                raise Failure(
                    ExitCode.CONFIG, f"tool extension {name} needs a handler and group list"
                )
        for label, entries in zip(("auth", "provisioning", "audit_sinks"), groups[1:], strict=True):
            for name, hook in entries.items():
                if not callable(hook):
                    raise Failure(ExitCode.CONFIG, f"{label} extension {name} must be callable")
        if set(instance.auth) & {"password", "oidc"}:
            raise Failure(ExitCode.CONFIG, "extension cannot replace a built-in identity backend")
        if config.mcp.auth not in {"password", "oidc"}:
            instance.identity_backend(config.mcp.auth)
        return instance

    def plugin_for(self, name):
        return self.tool_plugins.get(name, name if name in self.tools else None)

    def tools_for(self, groups: set[str], *, admin=False) -> dict:
        return {
            name: tool["handler"]
            for name, tool in self.tools.items()
            if ("*" in tool["groups"] or groups.intersection(tool["groups"]))
            and (not tool.get("admin", False) or admin)
        }

    def identity_backend(self, name: str):
        if name not in self.auth:
            raise Failure(
                ExitCode.CONFIG, f"identity backend {name} is not installed; install its extension"
            )
        return self.auth[name]

    def provision(self, name: str, *args, **kwargs):
        if name not in self.provisioning:
            raise Failure(ExitCode.CONFIG, f"provisioning extension {name} is not installed")
        return self.provisioning[name](*args, **kwargs)

    def emit_audit(self, row: dict) -> None:
        for sink in self.audit_sinks.values():
            sink(dict(row))
