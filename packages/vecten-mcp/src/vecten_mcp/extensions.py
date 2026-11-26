"""Installed MCP hooks; callers supply authenticated groups and persist audit rows first."""

from dataclasses import dataclass, field

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vecten_shared.config import VectenConfig
from vecten_shared.consumer_skills import skill_packages
from vecten_shared.extensions import load_group, selected_entry_points
from vecten_shared.names import group_name_problem
from vecten_shared.output import ExitCode, Failure

TOOL_KEYS = frozenset({"handler", "groups", "admin", "title", "description", "annotations"})
# The SDK's model drops keys it does not know, so a misspelled hint would reach no client.
ANNOTATION_KEYS = frozenset(f.alias or n for n, f in ToolAnnotations.model_fields.items())


class ToolMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    annotations: ToolAnnotations | None = None


def tool_metadata(name: str, tool: dict) -> ToolMetadata:
    unknown = sorted(map(str, set(tool) - TOOL_KEYS))
    annotations = tool.get("annotations")
    if isinstance(annotations, dict):
        unknown += [
            f"annotations.{key}"
            for key in sorted(map(str, annotations))
            if key not in ANNOTATION_KEYS
        ]
    if unknown:
        raise Failure(ExitCode.CONFIG, f"tool extension {name} has unknown {', '.join(unknown)}")
    supplied = {key: tool[key] for key in ToolMetadata.model_fields if key in tool}
    try:
        return ToolMetadata.model_validate(supplied, strict=True)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, error['loc']))} ({error['msg']})" for error in exc.errors()
        )
        raise Failure(ExitCode.CONFIG, f"tool extension {name} has invalid {problems}") from None


@dataclass(frozen=True)
class SkillDownload:
    groups: frozenset[str]
    archive: bytes


@dataclass
class McpExtensions:
    tools: dict
    auth: dict
    provisioning: dict
    audit_sinks: dict
    tool_plugins: dict = field(default_factory=dict)
    consumer_skills: dict[str, SkillDownload] = field(default_factory=dict)

    sign_in_hooks: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: VectenConfig) -> McpExtensions:
        groups = [
            load_group(config, f"vecten.mcp.{name}")
            for name in ("tools", "auth", "provisioning", "audit_sinks")
        ]
        instance = cls(*groups)
        if config.consumer_skills:
            archives = skill_packages(config, config.consumer_skills)
            instance.consumer_skills = {
                name: SkillDownload(frozenset(binding.groups), archives[name])
                for name, binding in config.consumer_skills.items()
            }
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
            tool_metadata(name, tool)
        for label, entries in zip(("auth", "provisioning", "audit_sinks"), groups[1:], strict=True):
            for name, hook in entries.items():
                if not callable(hook):
                    raise Failure(ExitCode.CONFIG, f"{label} extension {name} must be callable")
        owned = {"default"}
        for name, factory in load_group(config, "vecten.mcp.sign_in").items():
            hook = factory(config=config) if callable(factory) else None
            if not isinstance(hook, dict) or not callable(hook.get("handler")):
                raise Failure(ExitCode.CONFIG, f"sign-in extension {name} needs a handler")
            names = hook.get("groups")
            if not isinstance(names, list) or any(
                not isinstance(group, str) or group_name_problem(group) for group in names
            ):
                raise Failure(ExitCode.CONFIG, f"sign-in extension {name} needs a group list")
            if name == "local" or owned.intersection(names):
                raise Failure(
                    ExitCode.CONFIG,
                    f"sign-in extension {name} claims a reserved name or an owned group",
                )
            owned.update(names)
            instance.sign_in_hooks[name] = hook
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


class SignInHookUnavailable(Exception):
    """A verified sign-in cannot be completed because an installed access hook failed."""
