## ADDED Requirements

### Requirement: Declared secrets reach the component that reads them

Every declared secret SHALL reach its component the way a connector's secret reaches Airflow today. That includes a connector declaration's secrets, an extension manifest's secrets and the names in the top-level `secrets` list of `vecten.toml`. On the laptop the Compose file SHALL pass each declared name other than `MCP_*` to the Airflow services, and each declared `MCP_*` name to `mcp-server`, as `${NAME:-}`, so the stack starts before a secret is set and the read names it. On a VM, the bundle SHALL list the declared names other than `MCP_*` among the secrets the VM agent passes to the platform profile, with no change to the agent. The MCP server SHALL read declared `MCP_*` names from its scope through the provider. A declared name that collides with a variable the stack sets SHALL be refused before the Compose file is written.

#### Scenario: Extension secret on the laptop

- **WHEN** an extension's manifest declares `ACME_API_KEY` and `MCP_ACME_TOKEN`, both are in `.env`, and `vecten up` runs
- **THEN** the Airflow services' environment holds `ACME_API_KEY`, `mcp-server`'s holds `MCP_ACME_TOKEN`, and neither container holds the other's name

#### Scenario: Extension secret on a platform VM

- **WHEN** `ship` builds the platform host's bundle for a project whose extension declares `ACME_API_KEY`
- **THEN** `host.json` lists `ACME_API_KEY` beside the connector secrets, and the started Airflow services hold its value from the `platform` scope

#### Scenario: Collision with the stack

- **WHEN** a manifest declares `LOADER_PASSWORD`
- **THEN** `vecten lock` and `up` exit with the configuration error code naming the secret, and no Compose file is written

### Requirement: Settings and the extension record reach both runtimes

The Airflow services and `mcp-server` SHALL mount `vecten.toml` and `extensions.lock.json` read-only from the project on the laptop and from the bundle on a VM. Each SHALL set `VECTEN_ENV`, `VECTEN_PROJECT_ROOT` and `VECTEN_COMPONENT` (`platform` or `mcp`), so the shared read API resolves settings and secrets with no other input. No setting SHALL be passed as a container environment variable.

#### Scenario: Setting changed on the laptop

- **WHEN** a person changes `[settings.acme-resolution.env.local]` while the laptop stack runs
- **THEN** the next read in a DAG or a tool returns the new value without a rebuild

#### Scenario: Setting changed for prod

- **WHEN** a commit changes `[settings.acme-resolution.env.prod]` and is shipped to prod
- **THEN** prod's containers read the new value, and the previous bundle, if restored, brings back the previous one
