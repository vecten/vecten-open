## ADDED Requirements

### Requirement: Default read grants have one owner

When `mcp` is a component, the default group's reads on `core` and `marts` SHALL be owned by the `mcp` component's state database alone. The Snowflake module MUST NOT grant `analyst` `USAGE` or future `SELECT` on any schema, and the Postgres setup SQL that `ship` runs MUST NOT grant `analyst` `USAGE` or default privileges. On a deployment whose Terraform state holds the earlier `analyst` grant resources, the module SHALL drop them from state without revoking them.

#### Scenario: Snowflake deployment with the MCP upgraded

- **WHEN** `deploy` runs the upgraded module for an environment whose state holds `analyst_schema` and `analyst_future`
- **THEN** the plan revokes no grant from `ANALYST`, and the state no longer holds those resources

#### Scenario: Revoke survives a later deploy

- **WHEN** the default group's `marts` grant has been revoked through the MCP and `deploy --env prod` applies the Snowflake module again
- **THEN** `ANALYST` holds no `USAGE` and no future grant on `MARTS`

#### Scenario: Postgres ship with the MCP

- **WHEN** `ship` runs the setup SQL on a Postgres deployment with `mcp` in `components`
- **THEN** no statement grants `analyst` a privilege on `core` or `marts`

### Requirement: Platform-only deployments grant the default reads

When `mcp` is not a component, the Snowflake module and the Postgres setup SQL SHALL grant `analyst` `USAGE` and future `SELECT` on tables and views in `core` and `marts`, as before this change.

#### Scenario: Platform-only Snowflake deployment

- **WHEN** `deploy` runs for a Snowflake project without the `mcp` component
- **THEN** `ANALYST` holds `USAGE` and future `SELECT` on tables and views in `CORE` and `MARTS`
