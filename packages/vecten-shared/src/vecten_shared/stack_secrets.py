"""Secret names the stack generates or fixes itself, which no project or extension may declare."""

GENERATED = (
    "AIRFLOW_DB_PASSWORD",
    "MCP_STATE_DB_PASSWORD",
    "LOADER_PASSWORD",
    "PROVISIONER_PASSWORD",
    "IMPERSONATOR_PASSWORD",
    "POSTGRES_MASTER_PASSWORD",
    "AIRFLOW_ADMIN_PASSWORD",
    "AIRFLOW_API_SECRET_KEY",
    "AIRFLOW_JWT_SECRET",
)
FIXED = {"AIRFLOW_ADMIN_USER": "admin"}
# An older laptop's .env still holds these; they stay the stack's, never offered for a cloud.
RETIRED = ("AIRFLOW_BASIC_AUTH_PASSWORD", "AIRFLOW_BASIC_AUTH_USER")
STACK_MANAGED: frozenset[str] = frozenset({*GENERATED, *FIXED, *RETIRED, "AIRFLOW_FERNET_KEY"})
# Credentials the deployment itself sets and reads, beyond the laptop stack's own.
DEPLOYMENT_MANAGED: frozenset[str] = frozenset(
    {
        "PACKAGE_INDEX_URL",
        "MCP_PACKAGE_INDEX_URL",
        "MCP_PROVISIONER_PASSWORD",
        "MCP_IMPERSONATOR_PASSWORD",
        "MCP_SIGNING_KEY",
        "MCP_ADMIN_SETUP_TOKEN",
        "MCP_OIDC_CLIENT_SECRET",
        "ALERT_WEBHOOK_URL",
    }
)
