"""What an extension's manifest and a project may declare: secrets, and settings per environment.

A secret's name prefix decides which component receives it, as the scope rule decides which
host may read it. Settings are values in `vecten.toml` checked against the schema the
extension's manifest declares, as recorded in `extensions.lock.json`.
"""

from __future__ import annotations

import datetime
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vecten_shared.apps import APP_CREDENTIAL
from vecten_shared.names import secret_name_problem
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import scope_for
from vecten_shared.stack_secrets import (
    DEPLOYMENT_MANAGED,
    HOST_PREFIXES,
    PROCESS_NAMES,
    STACK_MANAGED,
)

if TYPE_CHECKING:
    from vecten_shared.config import VectenConfig
    from vecten_shared.extension_record import Extension

PROJECT = "project"
LOCAL = "local"
ENV_KEY = "env"
MANIFEST_KEYS = ("secrets", "settings")
SETTING_TYPES = ("string", "integer", "number", "boolean", "array", "object")
PROPERTY_KEYS = frozenset({"type", "default", "description", "enum", "minimum", "maximum", "items"})
SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties"})
_UNREADABLE_SCOPES = ("deploy", "admin", "setup")


@dataclass(frozen=True)
class Manifest:
    secrets: tuple[str, ...]
    settings: dict[str, Any] | None
    unknown: tuple[str, ...]


def secret_problem(name: str) -> str | None:
    """Why `name` cannot be declared, or None."""
    problem = secret_name_problem(name)
    if problem:
        return problem
    scope = scope_for(name)
    if scope in _UNREADABLE_SCOPES:
        return f"{name} is in the {scope} scope, which no host may read"
    if name in STACK_MANAGED or name in DEPLOYMENT_MANAGED or name in PROCESS_NAMES:
        return f"the stack manages {name}; declare a name of your own"
    if name.startswith(HOST_PREFIXES):
        prefixes = ", ".join(HOST_PREFIXES)
        return f"{name}: names starting with {prefixes} belong to the hosts and the stack"
    if APP_CREDENTIAL.fullmatch(name):
        return f"{name} is reserved for an analytics app's client credentials"
    return None


def component_for(name: str) -> str:
    """The component a declared secret is delivered to."""
    return "mcp" if scope_for(name) == "mcp" else "platform"


def _manifest_failure(extension: str, key: str, problem: str) -> Failure:
    return Failure(ExitCode.CONFIG, f"extension {extension}: manifest {key}: {problem}")


def _check_property(fail, name: str, spec: object) -> None:
    if not isinstance(spec, dict) or spec.get("type") not in SETTING_TYPES:
        raise fail(f"{name}: type must be one of {', '.join(SETTING_TYPES)}")
    if set(spec) - PROPERTY_KEYS:
        raise fail(f"{name}: unsupported keywords {', '.join(sorted(set(spec) - PROPERTY_KEYS))}")
    if "items" in spec:
        _check_property(fail, f"{name}.items", spec["items"])


def check_schema(extension: str, schema: object) -> dict[str, Any]:
    """The settings schema, if it stays inside the subset Vecten validates.

    The schema is recorded as JSON and checked again in every runtime, so it must survive a
    JSON round trip, be valid JSON Schema, and give every default a value its property accepts.
    """
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    def fail(problem: str) -> Failure:
        return _manifest_failure(extension, "settings", problem)

    try:
        if json.loads(json.dumps(schema, allow_nan=False)) != schema:
            raise ValueError("keys must be strings")
    except (TypeError, ValueError) as exc:
        raise fail(f"not plain JSON ({exc})") from None

    if not isinstance(schema, dict) or set(schema) - SCHEMA_KEYS:
        raise fail(f"a JSON Schema object with only {', '.join(sorted(SCHEMA_KEYS))}")
    if schema.get("type", "object") != "object":
        raise fail("type must be object")
    if schema.get("additionalProperties", False) is not False:
        raise fail("additionalProperties may only be false; unknown settings are refused")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise fail("properties must be a mapping")
    for name, spec in properties.items():
        if name == ENV_KEY:
            raise fail("a setting may not be named env, which holds per-environment overrides")
        _check_property(fail, name, spec)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise fail(exc.message) from None
    required = schema.get("required", [])
    if not set(required) <= set(properties):
        raise fail("required must list declared properties")
    for name, spec in properties.items():
        if "default" in spec:
            for error in Draft202012Validator(spec).iter_errors(spec["default"]):
                raise fail(f"{name}: default {spec['default']!r}: {error.message}")
    return schema


def parse_manifest(extension: str, value: object) -> Manifest:
    """A manifest entry point's object, a mapping or a zero-argument factory returning one."""
    if callable(value):
        value = value()
    if not isinstance(value, dict):
        raise _manifest_failure(extension, "manifest", "must be a mapping or return one")
    secrets = value.get("secrets", [])
    if not isinstance(secrets, list) or not all(isinstance(s, str) for s in secrets):
        raise _manifest_failure(extension, "secrets", "must be a list of secret names")
    for name in secrets:
        problem = secret_problem(name)
        if problem:
            raise _manifest_failure(extension, "secrets", problem)
    settings = value.get("settings")
    if settings is not None:
        settings = check_schema(extension, settings)
    unknown = tuple(sorted(k for k in value if k not in MANIFEST_KEYS))
    return Manifest(secrets=tuple(sorted(set(secrets))), settings=settings, unknown=unknown)


def is_toml_time(value: object) -> bool:
    """Whether `value` holds anything without a JSON type: a TOML date or time, inf or nan."""
    if isinstance(value, datetime.date | datetime.time):
        return True
    if isinstance(value, float) and not math.isfinite(value):
        return True
    if isinstance(value, dict):
        return any(is_toml_time(v) for v in value.values())
    if isinstance(value, list):
        return any(is_toml_time(v) for v in value)
    return False


def environments_named(config: VectenConfig) -> list[str]:
    """`local` and every environment a settings override names."""
    found = {LOCAL}
    for table in config.settings.values():
        found.update(table.get(ENV_KEY, {}))
    return sorted(found)


def effective_settings(
    config: VectenConfig, record: dict[str, Extension], namespace: str, env: str
) -> dict[str, Any]:
    """Defaults, then the namespace's values, then the environment's override, key by key."""
    values: dict[str, Any] = {}
    entry = record.get(namespace)
    if namespace != PROJECT and entry is not None and entry.settings:
        for key, spec in entry.settings.get("properties", {}).items():
            if "default" in spec:
                values[key] = spec["default"]
    table = config.settings.get(namespace, {})
    values.update({k: v for k, v in table.items() if k != ENV_KEY})
    values.update(table.get(ENV_KEY, {}).get(env, {}))
    return values


def _settings_failure(namespace: str, key: str, env: str, problem: str) -> Failure:
    return Failure(ExitCode.CONFIG, f"settings.{namespace}.{key} in {env}: {problem}")


def check_settings(config: VectenConfig, record: dict[str, Extension], env: str) -> None:
    """Every namespace's effective values in `env` against its extension's recorded schema."""
    from jsonschema import Draft202012Validator

    from vecten_shared.requirements import parse_requirement
    from vecten_shared.versions import normalize_name

    namespaces = set(config.settings) | {name for name, entry in record.items() if entry.settings}
    listed = {normalize_name(req.name) for req in map(parse_requirement, config.extensions) if req}
    for namespace in sorted(namespaces):
        values = effective_settings(config, record, namespace, env)
        if namespace == PROJECT:
            continue
        if namespace not in listed:
            continue
        entry = record.get(namespace)
        if entry is None and config.settings.get(namespace):
            raise Failure(
                ExitCode.CONFIG,
                f"settings.{namespace}: {namespace} is not in extensions.lock.json yet; "
                "run `vecten lock`",
            )
        if entry is None or entry.settings is None:
            if config.settings.get(namespace):
                raise Failure(
                    ExitCode.CONFIG,
                    f"settings.{namespace}: {namespace} declares no settings in its manifest",
                )
            continue
        schema = {**entry.settings, "type": "object", "additionalProperties": False}
        properties = schema.get("properties", {})
        for key in sorted(set(values) - set(properties)):
            raise _settings_failure(namespace, key, env, "unknown setting")
        for key in schema.get("required", []):
            if key not in values:
                raise _settings_failure(namespace, key, env, "required and not set")
        for error in sorted(Draft202012Validator(schema).iter_errors(values), key=str):
            key = str(error.path[0]) if error.path else "?"
            raise _settings_failure(namespace, key, env, error.message)
