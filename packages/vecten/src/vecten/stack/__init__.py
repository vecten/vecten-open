"""`up` and `down`: the local stack from vecten.toml, nothing in the project."""

from __future__ import annotations

from vecten_shared.config import VectenConfig

# Snowflake's External OAuth trusts only the deployed MCP's issuer, so a local MCP over
# Snowflake could sign in as its provisioner yet never run a person's query.
SNOWFLAKE_MCP_NOTE = (
    "the MCP runs only deployed on Snowflake: Snowflake's External OAuth trusts the deployed "
    "MCP's issuer, never a local one"
)


def local_components(config: VectenConfig) -> list[str]:
    """The components the local stack runs."""
    if config.warehouse.kind == "snowflake":
        return [c for c in config.components if c != "mcp"]
    return list(config.components)
