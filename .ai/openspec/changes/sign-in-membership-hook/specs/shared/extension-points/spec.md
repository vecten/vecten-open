## MODIFIED Requirements

### Requirement: Extensions are Python packages with per-layer entry points

An extension SHALL be a Python package that declares entry points in one or more of the groups `vecten.connectors`, `vecten.dbt_packages`, `vecten.dags`, `vecten.osi`, `vecten.mcp.tools`, `vecten.mcp.auth`, `vecten.mcp.sign_in`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks`, `vecten.skills`, `vecten.services` and `vecten.apps`, and MAY declare one `vecten.manifest` entry point stating the secrets and settings it needs. HTTP routes are declared as part of a service. Hooks for CLI commands are not offered and SHALL be added with their first consumer.

#### Scenario: Multi-layer extension

- **WHEN** an entity resolution extension declares `vecten.dbt_packages`, `vecten.dags`, `vecten.osi` and `vecten.mcp.tools`
- **THEN** its dbt models are built, its DAG runs, its Ossie document is loaded and its tool is offered, each through its own group

#### Scenario: Provisioning hook

- **WHEN** an extension declares `vecten.mcp.provisioning`
- **THEN** the server calls it in place of its own provisioner for creating warehouse users and granting per-session membership, which is how a just-in-time or cloud IAM credential installs

#### Scenario: Audit sink

- **WHEN** an extension declares `vecten.mcp.audit_sinks`
- **THEN** every audit row is also handed to it after the local write, which is how signed export installs

#### Scenario: Manifest beside layers

- **WHEN** an extension declares `vecten.dags` and `vecten.manifest`
- **THEN** its DAG runs and reads the settings and secrets its manifest declares

#### Scenario: Service beside a manifest

- **WHEN** an extension declares `vecten.services` and a manifest naming `ACME_BOT_TOKEN`
- **THEN** its bound service runs and reads `ACME_BOT_TOKEN` through the shared read API

#### Scenario: Dashboard beside models

- **WHEN** an extension declares `vecten.dbt_packages` and `vecten.apps`
- **THEN** its models are built and its bound app is published and listed

#### Scenario: Verified sign-in hook

- **WHEN** a configured extension declares `vecten.mcp.sign_in`
- **THEN** the MCP loads its factory and validates its group ownership before accepting sign-ins
