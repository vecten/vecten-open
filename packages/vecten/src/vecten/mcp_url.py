"""The URL of an environment's MCP: the one every CLI command reaches it at or names it by.

Terraform issues `mcp.<domain>` from the `domain` in `deploy/envs/<env>.tfvars`, the file the
project commits, so the CLI reads the name from there rather than from `.vecten/`, which a
fresh checkout lacks. `deploy` refuses a `prod` tfvars whose domain differs from vecten.toml's
`[deploy] domain`, so for `prod` vecten.toml gives the same name.
"""

from __future__ import annotations

from pathlib import Path

from vecten.deploy.tfvars import env_tfvars_path, no_tfvars, read_tfvars
from vecten_shared.config import VectenConfig
from vecten_shared.output import ExitCode, Failure

# `vecten up` serves the laptop's MCP here.
LOCAL_MCP_URL = "https://mcp.localhost:8443"


def environment_domain(project_root: Path, config: VectenConfig, env: str) -> str:
    """The domain deployed environment `env` serves its components under."""
    if config.deploy is None:
        raise Failure(
            ExitCode.PREREQUISITE,
            "the project has no deploy layer; run `vecten init --deploy` first",
        )
    if env == "prod":
        return config.deploy.domain
    path = env_tfvars_path(project_root, env)
    if not path.is_file():
        raise no_tfvars(env)
    domain = read_tfvars(path).get("domain")
    if not isinstance(domain, str) or not domain:
        raise Failure(ExitCode.CONFIG, f"deploy/envs/{env}.tfvars sets no domain")
    return domain


def mcp_url(project_root: Path, config: VectenConfig, env: str) -> str:
    """The MCP's base URL for `env`, which is also its OAuth issuer."""
    if env == "local":
        return LOCAL_MCP_URL
    return f"https://mcp.{environment_domain(project_root, config, env)}"
