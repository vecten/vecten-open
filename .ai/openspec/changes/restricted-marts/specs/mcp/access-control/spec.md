## MODIFIED Requirements

### Requirement: Default group maps to the analyst role
The deployment's default group SHALL map to the `analyst` warehouse role, which the bootstrap creates in a full deployment and the admin's printed script creates over an existing warehouse. Further groups SHALL map to roles the `provisioner` creates, named after the group unless the admin names the role when creating the group. A role name SHALL pass the same identifier checks as a group name and MUST NOT start with `admin_`, `svc_`, `pg_` or `p_`. When a role of the chosen name already exists, creation SHALL be refused naming both ways out: another role name, or binding the existing role.

#### Scenario: First person provisioned
- **WHEN** a person is provisioned with no group assigned
- **THEN** the person belongs to the default group
- **AND** the person's warehouse user holds `analyst`

#### Scenario: New group
- **WHEN** an admin creates the group `finance`
- **THEN** the provisioner creates a `finance` warehouse role and the group maps to it

#### Scenario: Group named like an existing role
- **WHEN** an admin creates the group `finance` and a role `finance` already exists in the warehouse
- **THEN** creation is refused, and the message names `--role` and `vecten group bind`
- **AND** `vecten group create finance --role fin_readers` creates the group with the role `fin_readers`

### Requirement: Grants are desired state, reconciled
Dataset, schema and area grants SHALL be stored in `mcp_state` as desired state. The server SHALL reconcile the warehouse against it on a schedule, fifteen minutes by default, on every model swap and, on Postgres, whenever the dbt DAG records a finished build. Reconciliation SHALL re-issue every desired grant. It SHALL also read the read grants each managed role actually holds and revoke those the state does not hold: `USAGE` on a schema, and current and future `SELECT` on any relation kind, in the convention schemas, `raw_<source>` schemas and areas of the environment's database. Managed roles are `analyst` and every role in the provisioner registry. A grant on any other schema or database, or to a role the server does not manage, MUST NOT be touched. Every grant added or revoked SHALL be logged and listed in access health. The setting `reconcile_drift` SHALL accept `revoke`, the default, and `report`, which lists the grants it would revoke without revoking them. The dbt project MUST NOT declare dbt `grants`, because they would revoke what the server grants.

#### Scenario: Table rebuilt by dbt
- **WHEN** dbt rebuilds the `orders` mart on Postgres and the `sales` role loses `SELECT` on it
- **THEN** the reconciliation that follows the recorded build restores the grant without admin action

#### Scenario: Reconcile on demand
- **WHEN** an admin runs the reconcile operation on the admin API
- **THEN** every grant in the state database is present in the warehouse when it returns
- **AND** no managed role holds a read grant in scope that the state does not hold

#### Scenario: Grant restored by an older deploy
- **WHEN** the state no longer holds the default group's `marts` grant and `analyst` holds `USAGE` and future `SELECT` on `marts`
- **THEN** the next reconciliation revokes them and access health lists both revocations

#### Scenario: Hand-made grant outside the scope
- **WHEN** an administrator has granted `analyst` `SELECT` on a table in a schema named `reference`
- **THEN** reconciliation leaves that grant in place

#### Scenario: Report only
- **WHEN** `reconcile_drift` is `report` and a group role holds a read grant the state does not hold
- **THEN** the grant stays, and access health lists it as drift

### Requirement: Schema grants beside dataset grants
An admin SHALL be able to grant a group a whole convention schema, `raw_<source>`, `staging`, `snapshots`, `core` or `marts`, and the server SHALL translate the grant into `USAGE` on the schema, `SELECT` on every relation in it, and a default or future grant so relations created later are covered. Revoking the grant MUST revoke all three. The grant SHALL be available from the admin panel and `vecten grant --schema`, both over the admin API, and never as an MCP tool.

#### Scenario: Granting core to a group
- **WHEN** an admin grants the `core` schema to the `sales` group
- **THEN** the `sales` warehouse role can read every table in `core`
- **AND** a table dbt creates in `core` on the next run is readable without a further grant

#### Scenario: Granting a connector's raw schema
- **WHEN** an admin grants `raw_hubspot` to the `sales` group
- **THEN** the `sales` warehouse role can read the raw HubSpot tables and no other raw schema

### Requirement: The default group reads the modelled layers
The default group SHALL be granted the `core` and `marts` schemas as schema grants, recorded once in `mcp_state` when the server first starts and applied by its first reconciliation. When `mcp` is a component, no deploy, ship or setup script SHALL grant `analyst` anything on `core` or `marts`, so the state database is the only owner and a revoke holds. `raw_<source>`, `staging`, `snapshots` and areas MUST NOT be granted to any group by default.

#### Scenario: First person asks beyond the metrics
- **WHEN** a person in the default group runs `sql-exec` against a `core` table
- **THEN** the query succeeds
- **AND** the same person's query against `raw_hubspot` is refused by the warehouse

#### Scenario: Revoke survives a deploy
- **WHEN** an admin revokes `marts` from the default group, and `deploy`, `ship` and the provisioner script then run
- **THEN** `analyst` holds no grant on `marts`
- **AND** a mart rebuilt by dbt afterwards is not readable by `analyst`

## ADDED Requirements

### Requirement: Restricted areas are closed until granted
A restricted area SHALL be a pair of schemas, `core_<area>` and `marts_<area>`, declared in `vecten.toml`. No role SHALL hold a read grant in an area until an admin grants the area to a group. An area grant SHALL be a schema grant on both schemas, applied and revoked together, from the admin panel and `vecten grant --area <area>`. Each schema SHALL get its own schema-level future grant, so a relation dbt creates or replaces in the area is readable by the granted groups and by no one else.

#### Scenario: Restricted mart rebuilt
- **WHEN** `finance` is granted the area `finance` and dbt rebuilds `marts_finance.finance_revenue` with `CREATE OR REPLACE`
- **THEN** members of `finance` can read the rebuilt table
- **AND** a person only in the default group is refused by the warehouse, with no reconciliation having run in between

#### Scenario: Area built before any grant
- **WHEN** dbt creates `marts_payroll` for a declared area that no group has been granted
- **THEN** no group's role can read anything in it

#### Scenario: Grant of an undeclared area
- **WHEN** an admin runs `vecten grant --area hr --group people` and `vecten.toml` declares no area `hr`
- **THEN** the grant is refused naming the missing `[areas.hr]` entry

### Requirement: Area access is declared with the area
Each `[areas.<name>]` entry in `vecten.toml` SHALL carry an `access.groups` list naming the groups that may read the area. When the list is absent, `next` SHALL report an `ask` item for it, resolved by `vecten answer`.

#### Scenario: Area declared without an access answer
- **WHEN** `[areas.finance]` is added with no `access.groups`
- **THEN** `next` reports an `ask` item asking which groups may read the `finance` area, listing the existing groups

### Requirement: Declared area access seeds the state database
After deploy, `next` SHALL report a `command` item that applies an area's `access.groups` with `vecten grant --area <name>`, creating a named group first when it does not exist. Grants applied this way are ordinary area grants in the state database, which the declaration seeds and MUST NOT overwrite.

#### Scenario: Access applied after deploy
- **WHEN** the deployment is up and `[areas.finance]` declares `access.groups = ["finance"]` and no `finance` group exists yet
- **THEN** `next` reports a `command` item that creates `finance` and grants it the `finance` area
- **AND** the item disappears once the grant is recorded in the state database

#### Scenario: Declaration and panel disagree
- **WHEN** an admin revokes the `finance` area from the `finance` group in the panel
- **THEN** the revocation stands and `next` does not re-apply the declaration

### Requirement: A group binds an existing role
An admin SHALL be able to bind an existing warehouse role to a group, which keeps its own provisioner-owned role. The server SHALL print the statement that grants the existing role to the group's role (on Snowflake `GRANT ROLE <existing> TO ROLE <group role>`, on Postgres `GRANT <existing> TO <group role> WITH INHERIT TRUE`) for an administrator to run, and SHALL record the binding once it finds the grant in place. Access health SHALL report a recorded binding whose grant is missing.

#### Scenario: Binding a finance role
- **WHEN** an admin binds `FINANCE_ANALYST` to the group `finance` and the administrator runs the printed statement
- **THEN** a member of `finance` can read what `FINANCE_ANALYST` reads

#### Scenario: Binding removed outside the server
- **WHEN** an administrator revokes `FINANCE_ANALYST` from the group's role
- **THEN** the next reconciliation lists the missing binding in access health

### Requirement: The server never manages a bound role
The server MUST NOT own, grant, alter or drop a bound role, and MUST NOT target it with a dataset, schema or area grant or with drift revocation. Deleting the group SHALL drop only the group's own role.

#### Scenario: Provisioner untouched by a binding
- **WHEN** `FINANCE_ANALYST` is bound to the group `finance`
- **THEN** `SHOW GRANTS TO ROLE PROVISIONER` shows no grant on `FINANCE_ANALYST`

#### Scenario: Group deleted
- **WHEN** the group `finance` with a bound role is deleted
- **THEN** the group's role is dropped and `FINANCE_ANALYST` still exists with its grants

### Requirement: Read grants cover every relation kind
A schema, area or dataset grant SHALL cover every relation kind that can hold a model: on Snowflake tables, views, materialized views, dynamic tables, external tables and Iceberg tables, each with its current and future grants; on Postgres tables, partitioned tables, views, materialized views and foreign tables. A grant the warehouse refuses for one kind SHALL be reported in access health with the warehouse's message.

#### Scenario: Dynamic table mart
- **WHEN** a group is granted `marts` and dbt builds a mart as a dynamic table
- **THEN** the group's role can read it without a further grant

#### Scenario: Dataset grant on a materialized view
- **WHEN** an admin grants a dataset whose source is a materialized view
- **THEN** the group's role holds `SELECT` on that materialized view
