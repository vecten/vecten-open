## MODIFIED Requirements

### Requirement: Analyst role is the default group's role

The `analyst` role SHALL be created by the setup script `ship` runs, or by the script `vecten mcp provisioner-script` prints for an existing warehouse, and the `mcp` component's default group SHALL map to it. When `mcp` is a component, neither script SHALL grant `analyst` any privilege on a schema or relation. Its reads come from the default group's grants in `mcp_state`, which the `mcp` component applies.

#### Scenario: First person provisioned

- **WHEN** a person is added to the default group
- **THEN** their warehouse account holds the `analyst` role

#### Scenario: Existing warehouse

- **WHEN** an MCP-only project is deployed over an existing warehouse
- **THEN** the printed script creates `analyst`, `provisioner` and, on Postgres, `impersonator`, plus the bounded grant helpers described below
- **AND** it grants `analyst` no privilege on `core` or `marts`

#### Scenario: Script rerun

- **WHEN** an administrator reruns the provisioner script after the default group's `marts` grant was revoked
- **THEN** `analyst` still holds no grant on `marts`

### Requirement: Provisioner scope

On Postgres the `provisioner` role SHALL hold `CREATEROLE`, `ADMIN` on `analyst` and on every role recorded in the provisioner registry in `mcp_state`, and, through default privileges set by the owner of the dbt schemas, `USAGE` on `core` and `marts` and `SELECT WITH GRANT OPTION` on their tables, plus USAGE on the protected `vecten_access` schema, SELECT without grant option on its helper registry, and EXECUTE on the registered owner functions, and nothing else. The `mcp` component MUST refuse to start when the provisioner holds anything outside that set.

The administrator-installed PostgreSQL helpers SHALL run as the corresponding object owner
with a fixed safe search path and SHALL restrict changes to USAGE, SELECT on tables,
partitioned tables, views, materialized views and foreign tables, and owner default SELECT
within their fixed schema. That schema is a convention schema, a `raw_<source>` schema, or a
schema matching `core_<area>` or `marts_<area>`. Targets SHALL be nonprivileged NOLOGIN reader
roles with bounded provisioner ADMIN, and never a role named `reader`. The registry and helper
schema MUST NOT be writable by managed roles or PUBLIC. New schemas or creator roles SHALL
require setup to be rerun; reconciliation MUST reject missing owner coverage before changing
grants. The managed setup SQL SHALL create the schemas of every area `vecten.toml` declares,
as `loader`, before it installs the helpers.

#### Scenario: Future table without owner membership

- **WHEN** a schema read grant is applied to a managed group and a registered creator later creates a table
- **THEN** the group can read the table, while provisioner holds no owner-role membership
- **AND** revocation removes existing and future SELECT from that group

#### Scenario: Correct scope

- **WHEN** the `mcp` component starts with a provisioner whose grants match the registry and the stated set
- **THEN** it serves traffic

#### Scenario: Creator ADMIN on its own roles

- **WHEN** the provisioner created `sales` and `jane_doe` and holds `ADMIN` on both
- **THEN** the startup check passes because both are in the registry

#### Scenario: Widened scope

- **WHEN** the provisioner is found at startup to hold `SUPERUSER`, `ACCOUNTADMIN`, or `ADMIN` on a role the registry does not name
- **THEN** the `mcp` component fails to start and names the offending grant

#### Scenario: Area declared before the first build

- **WHEN** `[areas.finance]` is declared and the next ship runs its setup SQL on a managed Postgres
- **THEN** `core_finance` and `marts_finance` exist, owned by `loader`, and each has a helper before dbt builds into it

### Requirement: Provisioner scope on Snowflake

On Snowflake the `provisioner` SHALL be a custom role holding `CREATE USER` and `CREATE ROLE` on the account, ownership of the roles it creates, and grant option on `analyst` and on `SELECT` over core/marts, plus USAGE on the protected `VECTEN_ACCESS` schema and its `READ_GRANT`, `READ_GRANTS` and `SOURCE_EXISTS` procedures, and nothing else. The administrator-owned `READ_GRANT` procedure SHALL limit operations to `USAGE` and `SELECT` grants in the configured database. It SHALL cover tables, views, materialized views, dynamic tables, external tables and Iceberg tables. The schema SHALL be a convention schema, a `RAW_<SOURCE>` schema, or one matching `^(CORE|MARTS)_[A-Z][A-Z0-9]*$`. The procedure SHALL accept as target only ANALYST or a role owned by PROVISIONER, and never `READER`. The administrator-owned `READ_GRANTS` procedure SHALL return the read grants and schema-level future grants that one such target holds in the configured database, and change nothing. Neither procedure SHALL delegate MANAGE GRANTS or ownership to provisioner. Each procedure SHALL report a version, and the `mcp` component SHALL refuse area grants and drift revocation while the installed version is older than its own, naming `vecten mcp provisioner-script`. Startup checks SHALL include future grants.

#### Scenario: Snowflake provisioner checked

- **WHEN** the `mcp` component starts against Snowflake
- **THEN** it verifies the provisioner's grants against this set and refuses to start on any addition

#### Scenario: Procedure older than the server

- **WHEN** the `mcp` component starts and `READ_GRANT` reports an older version
- **THEN** existing dataset and schema grants keep reconciling
- **AND** an area grant is refused, and access health names `vecten mcp provisioner-script` as the fix

#### Scenario: Target outside the bound

- **WHEN** `READ_GRANT` is called for a role that `PROVISIONER` does not own, such as a role bound to a group
- **THEN** it refuses and changes nothing
