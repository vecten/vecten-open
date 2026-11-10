# mcp/access-control Specification

## Purpose
Authorization by group: groups map to warehouse roles, admins grant Ossie datasets and convention schemas to groups, the default group reads the modelled layers, each connector declares who may read what it lands, and a per-group column deny list keeps unwanted columns out of what the agent sees.

## Requirements

### Requirement: A group maps to one warehouse role
Groups SHALL be the unit of authorization. Each group MUST map to exactly one warehouse role, and every person's warehouse user SHALL hold the roles of the groups the person belongs to.

#### Scenario: Person added to a group
- **WHEN** an admin adds a person to a group
- **THEN** the person's warehouse user is granted that group's warehouse role
- **AND** the person's next query can read what the role can read

#### Scenario: Person removed from a group
- **WHEN** an admin removes a person from a group
- **THEN** the group's warehouse role is revoked from the person's warehouse user

### Requirement: Default group maps to the analyst role
The deployment's default group SHALL map to the `analyst` warehouse role, which the bootstrap creates in a full deployment and the admin's printed script creates over an existing warehouse. Further groups SHALL map to roles the `provisioner` creates.

#### Scenario: First person provisioned
- **WHEN** a person is provisioned with no group assigned
- **THEN** the person belongs to the default group
- **AND** the person's warehouse user holds `analyst`

#### Scenario: New group
- **WHEN** an admin creates the group `finance`
- **THEN** the provisioner creates a `finance` warehouse role and the group maps to it

### Requirement: Dataset grants become warehouse SELECT grants
An admin SHALL grant Ossie datasets to groups, and the server SHALL translate each grant into `GRANT SELECT` on the dataset's source for the group's warehouse role. Revoking the grant MUST revoke the warehouse grant.

#### Scenario: Granting a dataset to a group
- **WHEN** an admin grants the `orders` dataset to the `sales` group
- **THEN** the `sales` warehouse role holds `SELECT` on the source of `orders`

#### Scenario: Revoking
- **WHEN** the grant is revoked
- **THEN** the `sales` warehouse role no longer holds `SELECT` on that source

### Requirement: Grants are desired state, reconciled
Dataset and schema grants SHALL be stored in `mcp_state` as desired state. The server SHALL reconcile the warehouse against it on a schedule, fifteen minutes by default, and on every model swap, re-issuing any grant a dbt rebuild dropped and revoking any the state no longer holds. The dbt project MUST NOT declare dbt `grants`, because they would revoke what the server grants.

#### Scenario: Table rebuilt by dbt
- **WHEN** dbt rebuilds the `orders` mart and the `sales` role loses `SELECT` on it
- **THEN** the next reconciliation restores the grant without admin action

#### Scenario: Reconcile on demand
- **WHEN** an admin runs the reconcile operation on the admin API
- **THEN** every grant in the state database is present in the warehouse when it returns

### Requirement: Refused grants say why
Each grant or revoke the warehouse refuses SHALL be logged with its group, target and the warehouse's error, and the last reconciliation's failures SHALL be readable from the admin API as access health. A grant refused on the admin API SHALL answer with the warehouse's reason.

#### Scenario: Grant refused by the warehouse
- **WHEN** reconciliation re-issues a dataset grant and Snowflake's grant procedure refuses its target role
- **THEN** the log names the group, the dataset and Snowflake's message, and access health lists the failure until a reconciliation applies it

### Requirement: The warehouse is the only enforcement point
Access to data SHALL be enforced by the warehouse through the person's warehouse user. Row filters and column masking defined in the warehouse MUST apply unchanged because every query runs as the person; the server SHALL NOT re-implement them.

#### Scenario: Warehouse row policy applies
- **WHEN** the warehouse has a row-level policy limiting a person to one region and the person runs `sql-exec` on that table
- **THEN** the result contains only that region's rows
- **AND** the server added no filter of its own

### Requirement: Per-group column deny list
The server SHALL keep a per-group list of denied columns. A column denied for any of a person's groups MUST be omitted from `describe`, `search` and the metric compiler for that person. The audit still names it, as the `mcp/audit` spec states, and redacts the values compared with it.

#### Scenario: Denied column in describe and search
- **WHEN** `email` on `customers` is denied for the `analysts` group and an analyst calls `describe` or searches for "email"
- **THEN** the `email` field does not appear in the response

#### Scenario: Denied column requested from the compiler
- **WHEN** an analyst passes a denied column as a dimension or filter field to `query_metrics`
- **THEN** the tool refuses with reason `unknown_field`

### Requirement: Deny list hides but does not enforce
The deny list SHALL NOT be presented as a security boundary. A denied column that the warehouse still allows SHALL remain selectable through `sql-exec`, and the documentation and panel MUST state that the warehouse is the enforcement point.

#### Scenario: Denied column selected through agent SQL
- **WHEN** a person selects a denied column directly through `sql-exec` and the warehouse permits it
- **THEN** the query succeeds
- **AND** the audit row's column list names the denied column

### Requirement: Grants and deny lists live in the state database
Group definitions, dataset grants and deny lists SHALL be stored in the state database and MUST NOT be written into the Ossie model.

#### Scenario: Model reload after a grant change
- **WHEN** an admin changes grants and the model is then reloaded from git
- **THEN** the grants are unchanged by the reload

### Requirement: Schema grants beside dataset grants
An admin SHALL be able to grant a group a whole convention schema, `raw_<source>`, `staging`, `snapshots`, `core` or `marts`, and the server SHALL translate the grant into `USAGE` on the schema, `SELECT` on every table and view in it, and a default or future grant so tables created later are covered. Revoking the grant MUST revoke all three. The grant SHALL be available from the admin panel and `vecten grant --schema`, both over the admin API, and never as an MCP tool.

#### Scenario: Granting core to a group
- **WHEN** an admin grants the `core` schema to the `sales` group
- **THEN** the `sales` warehouse role can read every table in `core`
- **AND** a table dbt creates in `core` on the next run is readable without a further grant

#### Scenario: Granting a connector's raw schema
- **WHEN** an admin grants `raw_hubspot` to the `sales` group
- **THEN** the `sales` warehouse role can read the raw HubSpot tables and no other raw schema

### Requirement: The default group reads the modelled layers
The setup script, and `vecten mcp provisioner-script` over an existing warehouse, SHALL grant the `analyst` role the `core` and `marts` schemas as schema grants. `raw_<source>`, `staging` and `snapshots` MUST NOT be granted to any group by default.

#### Scenario: First person asks beyond the metrics
- **WHEN** a person in the default group runs `sql-exec` against a `core` table
- **THEN** the query succeeds
- **AND** the same person's query against `raw_hubspot` is refused by the warehouse

### Requirement: Connector access is declared when the connector is added
Each connector declaration in `vecten.toml` SHALL carry an `access.groups` list naming the groups that may read its `raw_<source>` schema. When the list is absent, `next` SHALL report an `ask` item for it, resolved by `vecten answer`.

#### Scenario: Connector added without an access answer
- **WHEN** `connector add hubspot` has rendered the connector and `access.groups` is not set
- **THEN** `next` reports an `ask` item asking which groups may read the HubSpot raw data, with the existing groups listed and `default` offered

### Requirement: Declared connector access seeds the state database
After deploy, `next` SHALL report a `command` item that applies each connector's `access.groups` with `vecten grant --schema raw_<source>`, creating a named group through `vecten group` first when it does not exist. Grants applied this way are ordinary schema grants in the state database, which the declaration seeds and MUST NOT overwrite.

#### Scenario: Access applied after deploy
- **WHEN** the deployment is up and `hubspot` declares `access.groups = ["sales"]` and no `sales` group exists yet
- **THEN** `next` reports a `command` item that creates `sales` and grants it `raw_hubspot`
- **AND** the item disappears once the grant is recorded in the state database

#### Scenario: Declaration and panel disagree
- **WHEN** an admin later revokes `raw_hubspot` from `sales` in the panel
- **THEN** the revocation stands and `next` does not re-apply the declaration
