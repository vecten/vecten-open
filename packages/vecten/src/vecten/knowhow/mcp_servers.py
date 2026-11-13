"""The `.mcp.json` document a platform project carries: the Airflow and dbt MCP servers."""

from __future__ import annotations

from typing import Any

from vecten.stack.compose import LOCAL_AIRFLOW_API_PORT
from vecten_shared.stack_secrets import FIXED
from vecten_shared.versions import VERSIONS, minor_constraint


def mcp_servers_document() -> dict[str, Any]:
    # The file is committed, so `env` holds `${VAR}` references the client expands, never values;
    # the defaults are the local stack's, so only the password and dbt's path need exporting.
    airflow_url = f"http://127.0.0.1:{LOCAL_AIRFLOW_API_PORT}"
    airflow_user = FIXED["AIRFLOW_ADMIN_USER"]
    return {
        "mcpServers": {
            "airflow": {
                "command": "uvx",
                "args": [
                    f"astro-airflow-mcp{minor_constraint(VERSIONS.airflow_mcp_server)}",
                    "--transport",
                    "stdio",
                ],
                "env": {
                    "AIRFLOW_API_URL": f"${{AIRFLOW_API_URL:-{airflow_url}}}",
                    "AIRFLOW_USERNAME": f"${{AIRFLOW_USERNAME:-{airflow_user}}}",
                    "AIRFLOW_PASSWORD": "${AIRFLOW_PASSWORD}",
                },
            },
            "dbt": {
                "command": "uvx",
                "args": [f"dbt-mcp{minor_constraint(VERSIONS.dbt_mcp)}"],
                "env": {
                    "DBT_PROJECT_DIR": "${CLAUDE_PROJECT_DIR:-.}/dbt",
                    "DBT_PATH": "${DBT_PATH}",
                },
            },
        }
    }
