# shared/identity-contract Specification

## Purpose
The warehouse naming and role conventions the `platform` and `mcp` components both follow, so that one human is one principal keyed on their login identity and one warehouse account, and every account's creator is unambiguous. The Airflow UI is outside this contract: it sits behind the platform's own authentication, which the `platform/platform-stack` spec defines.

## Requirements

### Requirement: One principal per human, keyed on login identity

Every human SHALL be exactly one principal, keyed on the identity backend's issuer and subject, or on the email address under the password backend, and carrying email, display name, warehouse name and an admin flag. The `mcp` component SHALL create the warehouse account, or adopt an existing account of that name when it is bound to no other principal, and the person and every agent acting for them SHALL use it.

#### Scenario: New person

- **WHEN** an admin adds Jane Doe with her email and no account `jane_doe` exists
- **THEN** a principal keyed on her login identity exists and the `mcp` component creates `jane_doe` in the warehouse

### Requirement: Warehouse name derived by one transliteration

The warehouse name SHALL be derived once from the display name: lower-case ASCII, non-ASCII letters mapped to their base letters, one underscore between first and last name, truncated to fit the 63-byte Postgres limit, with a numeric suffix on collision. An admin MAY override it at creation.

#### Scenario: Two people with one name

- **WHEN** a second Jan Kowalski is added while `jan_kowalski` is bound to the first
- **THEN** the second gets `jan_kowalski_2` and the admin is told

#### Scenario: Diacritics

- **WHEN** Łukasz Żółć is added
- **THEN** his warehouse name is `lukasz_zolc` on Postgres and `LUKASZ_ZOLC` as Snowflake reports it

#### Scenario: Existing account

- **WHEN** an admin adds Jane Doe and `jane_doe` already exists and is bound to no principal
- **THEN** the `mcp` component shows that the account exists, with its roles and last login, and adopts it only on the admin's confirmation

#### Scenario: Account bound to someone else

- **WHEN** an admin adds a person whose derived name is already bound to another principal
- **THEN** the request is refused naming the other principal, and no adoption is offered

#### Scenario: Agent acting for a person

- **WHEN** a person's Claude Desktop and Claude Code sessions both run queries
- **THEN** the warehouse enforces every query as `jane_doe` and the audit names the same principal

### Requirement: Admin warehouse accounts

A human admin's warehouse administration account SHALL be named `admin_<firstname>`, SHALL be created by `vecten user add-admin <firstname>` over the master credential, and MUST NOT be created, held or used by the `mcp` component. It is for direct warehouse work only. Being an MCP admin is a flag on the principal, never a warehouse role or a group.

#### Scenario: Admin created

- **WHEN** `vecten user add-admin jane` runs
- **THEN** `admin_jane` exists in the warehouse with administrative rights
- **AND** the `mcp` component holds no credential for it

#### Scenario: Admin also uses the MCP

- **WHEN** Jane is an MCP admin and asks a question through the MCP
- **THEN** the query runs as `jane_doe`, never as `admin_jane`

#### Scenario: Admin flag has no warehouse footprint

- **WHEN** a principal's admin flag is set
- **THEN** no warehouse role or grant changes

### Requirement: Service principals

An independent agent or service SHALL have a warehouse account named `svc_<name>`, created by the `mcp` component, and SHALL authenticate to the MCP with the client credentials grant.

#### Scenario: Dashboard host

- **WHEN** an admin creates a service principal `dashboards`
- **THEN** the warehouse holds `svc_dashboards`
- **AND** a client authenticating with its client credentials runs queries as `svc_dashboards`

### Requirement: Platform roles

The `loader` role SHALL be created by the `platform` deploy for Airflow, dlt and dbt. It MUST NOT be an MCP principal and MUST NOT appear in the `mcp` component's users or groups.

#### Scenario: Loader absent from the MCP

- **WHEN** an admin lists users and groups in the `mcp` component
- **THEN** `loader` is not among them

### Requirement: Analyst role is the default group's role

The `analyst` role SHALL be created by the setup script `ship` runs, or by the script `vecten mcp provisioner-script` prints for an existing warehouse, and the `mcp` component's default group SHALL map to it.

#### Scenario: First person provisioned

- **WHEN** a person is added to the default group
- **THEN** their warehouse account holds the `analyst` role

#### Scenario: Existing warehouse

- **WHEN** an MCP-only project is deployed over an existing warehouse
- **THEN** the printed script creates `analyst`, `provisioner` and, on Postgres, `impersonator`, plus the bounded grant helpers described below

### Requirement: Provisioner scope

On Postgres the `provisioner` role SHALL hold `CREATEROLE`, `ADMIN` on `analyst` and on every role recorded in the provisioner registry in `mcp_state`, and, through default privileges set by the owner of the dbt schemas, `USAGE` on `core` and `marts` and `SELECT WITH GRANT OPTION` on their tables, plus USAGE on the protected `vecten_access` schema, SELECT without grant option on its helper registry, and EXECUTE on the registered owner functions, and nothing else. The `mcp` component MUST refuse to start when the provisioner holds anything outside that set.

The administrator-installed PostgreSQL helpers SHALL run as the corresponding object owner
with a fixed safe search path and SHALL restrict changes to USAGE, table/view SELECT and
owner default SELECT within their fixed convention schema. Targets SHALL be nonprivileged
NOLOGIN reader roles with bounded provisioner ADMIN. The registry and helper schema MUST NOT
be writable by managed roles or PUBLIC. New schemas or creator roles SHALL require setup to
be rerun; reconciliation MUST reject missing owner coverage before changing grants.

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

### Requirement: Provisioner scope on Snowflake

On Snowflake the `provisioner` SHALL be a custom role holding `CREATE USER` and `CREATE ROLE` on the account, ownership of the roles it creates, and grant option on `analyst` and on `SELECT` over core/marts, plus USAGE on the protected `VECTEN_ACCESS` schema and its `READ_GRANT` procedure, and nothing else. The administrator-owned procedure SHALL limit operations to read grants on convention schemas in the configured database and SHALL accept only ANALYST or roles owned by PROVISIONER. It SHALL NOT delegate MANAGE GRANTS or ownership to provisioner. Startup checks SHALL include future grants.

#### Scenario: Snowflake provisioner checked

- **WHEN** the `mcp` component starts against Snowflake
- **THEN** it verifies the provisioner's grants against this set and refuses to start on any addition

### Requirement: Warehouse secrets the server holds

The server SHALL hold no per-principal warehouse secret. The environment's secrets store SHALL hold `mcp/provisioner`, on Postgres `mcp/impersonator`, and the OAuth signing key at `mcp/signing_key`, and nothing else for the `mcp` component's warehouse access. Each SHALL be rotated on a schedule.

#### Scenario: State database inspected

- **WHEN** `mcp_state` is read directly after people have been provisioned
- **THEN** it contains no password, key or token material

#### Scenario: Secrets store listed

- **WHEN** the secrets store is listed after people have been provisioned
- **THEN** no entry is named after a person

### Requirement: Reserved name prefixes

The `mcp` component SHALL refuse to create a human principal whose warehouse name starts with `admin_` or `svc_`, and SHALL require the `svc_` prefix on every service principal.

#### Scenario: Human named like an admin

- **WHEN** an admin tries to add a human whose derived name would be `admin_jane`
- **THEN** the request is refused naming the reserved prefix

#### Scenario: Service without prefix

- **WHEN** a service principal is requested with a name that lacks `svc_`
- **THEN** the component applies the prefix and reports the resulting name
