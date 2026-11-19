## ADDED Requirements

### Requirement: The contract's names on Snowflake

On Snowflake the roles and accounts this contract names, `analyst`, `loader`, `provisioner`, a group's role and a principal's warehouse account, SHALL be the objects whose names the `shared/deploy` spec's "One source for Snowflake names" derives for the environment. The warehouse name a principal is given SHALL still be derived as "Warehouse name derived by one transliteration" states, and the reserved prefixes SHALL apply to it; with a shared account the Snowflake user SHALL be that name followed by the environment's suffix.

#### Scenario: Person in a shared account

- **WHEN** Jane Doe is added in environment `dev` of project `acme` with a shared account
- **THEN** her warehouse name is `jane_doe`, and her Snowflake user is `JANE_DOE_ACME_DEV`

#### Scenario: Two environments, one person

- **WHEN** Jane Doe is added in `dev` and in `prod` of a project whose environments share an account
- **THEN** two Snowflake users exist, `JANE_DOE_ACME_DEV` and `JANE_DOE_ACME_PROD`, each holding only its environment's roles

## MODIFIED Requirements

### Requirement: Provisioner scope on Snowflake

On Snowflake the environment's `provisioner` SHALL be a custom role holding `CREATE USER` and `CREATE ROLE` on the account, ownership of the roles and users it creates, and grant option on the environment's `analyst` role and on `SELECT` over core/marts in the environment's database, plus USAGE on the environment's database and warehouse, USAGE on the protected `VECTEN_ACCESS` schema in that database and its `READ_GRANT` and `SOURCE_EXISTS` procedures, and, with a shared account, `APPLY` on the environment's authentication policy, and nothing else. The administrator-owned procedure SHALL limit operations to read grants on convention schemas in the environment's database and SHALL accept only the environment's `analyst` role or roles owned by the environment's `provisioner`; with a shared account it SHALL also refuse any role whose name does not end in the environment's suffix. It SHALL NOT delegate MANAGE GRANTS or ownership to the provisioner. Startup checks SHALL include future grants, and SHALL compare names with the environment's names, never with fixed ones.

#### Scenario: Snowflake provisioner checked

- **WHEN** the `mcp` component starts against Snowflake
- **THEN** it verifies the provisioner's grants against this set and refuses to start on any addition

#### Scenario: Provisioner reaching another environment

- **WHEN** `dev`'s MCP starts with a shared account and `PROVISIONER_ACME_DEV` holds USAGE on the database `ACME_PROD`
- **THEN** it refuses to start, naming the grant

#### Scenario: Grant to another environment's role

- **WHEN** `dev`'s `READ_GRANT` is called with the target `FINANCE_ACME_PROD`
- **THEN** it refuses the target before checking ownership, and no grant changes
