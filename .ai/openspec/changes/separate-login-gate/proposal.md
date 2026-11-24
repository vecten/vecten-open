# Separate the service-host login gate

Service hostnames send each authenticated request through a gate. Run that gate independently of the MCP process with the shared state database and the same release image. Human authentication and initial code issuance stay on the MCP issuer. Existing sessions can continue through an MCP restart. The centralized gate on the MCP host keeps the state credential off split platform hosts.

Airflow will sign in directly with its identity provider in #5. Airflow administrators can then use the UI during an MCP outage.

This change modifies the service gate requirements introduced by `extension-services`. Archive that change before this one. The gate uses a dedicated PostgreSQL login, `mcp_gate`, with row policies on shared state records.
