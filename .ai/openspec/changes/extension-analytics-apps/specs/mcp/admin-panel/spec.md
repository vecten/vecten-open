## ADDED Requirements

### Requirement: Analytics screen

The panel SHALL list every bound app under "Analytics" with its title, extension and version, kind, URL, bound and admitted groups, declared datasets, its service's last health reported by `ship` for a `container` app or its publication state for a `snowflake` app, and its access-health failures. The screen SHALL be read-only and SHALL name `[apps.<extension>.<app>]` in `vecten.toml` as the place to change an app's groups.

#### Scenario: Admin reviews installed apps

- **WHEN** an admin opens Analytics in a deployment with one container app and one unpublished Snowflake app
- **THEN** both are listed, the container app with its URL and admitted groups, the Snowflake app as not published with the reason

#### Scenario: Admin wants to change viewers

- **WHEN** an admin opens an app on the Analytics screen
- **THEN** the screen shows the binding's location in `vecten.toml` and offers no edit

### Requirement: People see the apps they may open

The server SHALL serve `/apps` on the MCP host to any signed-in person, listing with links the bound apps that admit one of the person's current groups and nothing about any other app.

#### Scenario: Analyst opens the list

- **WHEN** a person in `sales` only opens `https://mcp.<domain>/apps` in a deployment with a sales app and a finance app
- **THEN** the page lists the sales app with its link and does not mention the finance app
