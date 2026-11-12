"""`secret set` and `secret list` over the provider; values never pass through argv or stdout."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from vecten_shared.config import VectenConfig, load_config
from vecten_shared.names import check_env_name, check_secret_name
from vecten_shared.output import ExitCode, Failure
from vecten_shared.provider import Provider, get_provider
from vecten_shared.provider.local import LocalProvider
from vecten_shared.records import read_record, write_record
from vecten_shared.stack_secrets import STACK_MANAGED

T = TypeVar("T")

# `vecten mcp introspect` signs in with the existing warehouse's administrator on this machine;
# no deployed component reads it, so a cloud copy would only widen where it can leak.
LAPTOP_ONLY = frozenset({"WAREHOUSE_ADMIN_PASSWORD", "WAREHOUSE_ADMIN_PRIVATE_KEY"})


def provisioner_secret_names(config: VectenConfig) -> list[str]:
    """The MCP's warehouse logins an MCP-only project stores, locally and per environment."""
    # Only a Postgres warehouse has the impersonator login; Snowflake has the provisioner alone.
    if config.warehouse.kind == "postgres":
        return ["MCP_PROVISIONER_PASSWORD", "MCP_IMPERSONATOR_PASSWORD"]
    return ["MCP_PROVISIONER_PRIVATE_KEY"]


def read_value(stdin_text: str | None, prompt: Callable[[], str]) -> str:
    if stdin_text is None:
        value = prompt()
    else:
        value = stdin_text[:-2] if stdin_text.endswith("\r\n") else stdin_text.removesuffix("\n")
    if value == "":
        raise Failure(ExitCode.USAGE, "the secret value is empty")
    return value


def check_request(name: str, env: str | None, from_local: bool) -> None:
    check_secret_name(name)
    if env is not None:
        check_env_name(env)
    if from_local and env is None:
        raise Failure(
            ExitCode.USAGE, "--from-local copies a local secret to an environment; add --env <env>"
        )
    if env is not None and name in LAPTOP_ONLY:
        raise Failure(
            ExitCode.REFUSED,
            f"{name} stays on this machine: the deployment never uses it",
        )
    # The stack's own credentials share .env with connector secrets; a write here would
    # replace one the running stack depends on.
    if env is None and name in STACK_MANAGED:
        raise Failure(
            ExitCode.REFUSED,
            f"{name} is managed by `vecten up`; to change it by hand, edit the project's .env file",
        )


@dataclass(frozen=True)
class CloudStore:
    cloud: str
    provider: Provider

    def call(self, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except Failure:
            raise
        except Exception as exc:
            # A provider's exception text may quote the secret value, so only the type is shown.
            raise Failure(
                ExitCode.TOOL_FAILED,
                f"the {self.cloud} secrets store failed: {type(exc).__name__}",
            ) from None


def cloud_store(project_root: Path, env: str) -> CloudStore:
    check_env_name(env)
    config = load_config(project_root)
    if config.deploy is None:
        raise Failure(
            ExitCode.REFUSED,
            f"environment {env!r} has no cloud yet; run `vecten init --deploy` first",
        )
    # The runtime classes refuse most writes; the cloud's control-plane class holds the slot
    # once registered, and the import stays here so that importing the CLI loads no cloud SDK.
    from vecten.deploy.control import register_cli_provider

    register_cli_provider(config.deploy.cloud)
    return CloudStore(config.deploy.cloud, get_provider(config.deploy.cloud, project_root, env))


def cloud_provider(project_root: Path, env: str) -> Provider:
    return cloud_store(project_root, env).provider


def set_secret(
    project_root: Path, name: str, value: str | None, env: str | None, from_local: bool
) -> dict:
    check_request(name, env, from_local)
    store = None if env is None else cloud_store(project_root, env)
    local = LocalProvider(project_root, inside_compose=False)
    if from_local:
        value = local.get_secret(name)
    if value is None:
        raise Failure(ExitCode.INTERNAL, "no secret value")
    if store is None:
        local.put_secret(name, value)
        return {"name": name, "env": "local", "copied": False}
    store.call(lambda: store.provider.put_secret(name, value))
    record_cloud_write(project_root, env, name, from_local)
    config = load_config(project_root)
    required = set(provisioner_secret_names(config))
    if not config.has("platform") and required <= recorded_secret_names(project_root, env):
        from vecten_shared.config import EnvironmentFacts, save_config

        config.environments.setdefault(env, EnvironmentFacts()).provisioner_secrets_set = True
        save_config(project_root, config)
    return {"name": name, "env": env, "copied": from_local}


def record_cloud_write(project_root: Path, env: str, name: str, from_local: bool) -> None:
    """Record the name of a secret written to `env`, never its value.

    `copied` holds names taken from .env and `stored` names typed or piped in; `next` reads
    a name in either as present in the environment.
    """
    previous = read_record(project_root, f"secrets-{env}") or {}
    names = {key: set(previous.get(key, [])) for key in ("copied", "stored")}
    names["copied" if from_local else "stored"].add(name)
    write_record(
        project_root,
        f"secrets-{env}",
        {"env": env, "copied": sorted(names["copied"]), "stored": sorted(names["stored"])},
    )


def recorded_secret_names(project_root: Path, env: str) -> set[str]:
    """Names `secret set --env` wrote to `env` from this checkout, copied or stored."""
    record = read_record(project_root, f"secrets-{env}") or {}
    return set(record.get("copied", [])) | set(record.get("stored", []))


def list_secret_names(project_root: Path, env: str | None) -> list[str]:
    if env is None:
        return LocalProvider(project_root, inside_compose=False).list_secrets()
    store = cloud_store(project_root, env)
    return sorted(store.call(store.provider.list_secrets))
