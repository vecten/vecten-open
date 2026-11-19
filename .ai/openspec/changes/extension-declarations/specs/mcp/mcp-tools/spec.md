## ADDED Requirements

### Requirement: Tool handlers read settings and secrets through the shared API

A tool handler from an extension SHALL read its settings and secrets through `vecten_shared.context.extension(<its distribution>)`, as the `shared/extension-points` spec states. Inside `mcp-server` it SHALL be able to read only the `MCP_*` secrets its manifest declares. The server SHALL have checked the extension's settings for its environment before it serves its first request.

#### Scenario: Tool with a cap

- **WHEN** a `crm-search` tool from `acme-crm` reads `extension("acme-crm").settings["max_rows"]` and `.secret("MCP_CRM_TOKEN")`
- **THEN** it gets the environment's effective value and the token from the `mcp` scope, on the laptop and on a VM alike

#### Scenario: Invalid setting at start

- **WHEN** prod's MCP server starts with `[settings.acme-crm.env.prod]` setting `max_rows = "many"`, which `ship`'s own check would normally have refused first
- **THEN** the server logs the invalid key and environment and never reports healthy, so `ship` restores the previous bundle
