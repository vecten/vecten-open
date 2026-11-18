## ADDED Requirements

### Requirement: provisioner-script in a shared Snowflake account

With a shared Snowflake account, `vecten mcp provisioner-script --env <env>` SHALL print SQL that creates, with the environment's names: the `analyst` and `provisioner` roles, the provisioner's service user with the environment's `MCP_PROVISIONER_PRIVATE_KEY` public key, the `VECTEN_ACCESS` procedures in the environment's database, the authentication policy `MCP_SIGN_IN` in that schema with `AUTHENTICATION_METHODS = ('OAUTH')` and `SECURITY_INTEGRATIONS` naming the environment's integration, `APPLY` on it for the `provisioner`, and the environment's External OAuth integration with the environment's issuer, JWKS URL and the account's audience. Every object SHALL carry the comment `vecten <project> <env>`, and the script SHALL stop, changing nothing more, when an object it would create exists with another comment. It SHALL name no fixed role, user or integration, and SHALL alter no object of another environment.

#### Scenario: Script for dev in a shared account

- **WHEN** `vecten mcp provisioner-script --env dev` runs for project `acme` with a shared account
- **THEN** the SQL creates `ANALYST_ACME_DEV`, `PROVISIONER_ACME_DEV` (role and user), `ACME_DEV.VECTEN_ACCESS.MCP_SIGN_IN` and `EXTERNAL_OAUTH_ACME_DEV` with `https://mcp.<dev domain>` as its issuer
- **AND** it holds no statement naming `ANALYST`, `PROVISIONER`, `VECTEN_PROVISIONER`, `VECTEN_EXTERNAL_OAUTH` or another environment's suffix

#### Scenario: Name held by another project

- **WHEN** the script runs and `ANALYST_ACME_DEV` exists with the comment of another project
- **THEN** the script stops at that object, naming it, and creates and alters nothing after it

### Requirement: destroy names what stays in a shared Snowflake account

On a Snowflake root with a shared account, `destroy --env <env>` SHALL report the objects outside Terraform that stay, the environment's provisioner user, its integration and the users and roles its `provisioner` owns, and with `--delete-data <env>` SHALL refuse as the `shared/deploy` spec's "Snowflake module" states, printing the `DROP` statements for an administrator.

#### Scenario: Destroy without data deletion

- **WHEN** `destroy --env dev --confirm dev` runs with a shared account
- **THEN** the result says the database was kept, and lists `PROVISIONER_ACME_DEV`, `EXTERNAL_OAUTH_ACME_DEV` and the users and roles `PROVISIONER_ACME_DEV` owns as kept in Snowflake

## MODIFIED Requirements

### Requirement: mcp provisioner-script

`vecten mcp provisioner-script` SHALL print, for an MCP-only project over an existing warehouse, the SQL an admin runs there to create the roles the identity contract names with exactly their grants, followed by the `secret set` commands that store the provisioner and, on Postgres, impersonator credentials. On Snowflake, unless `--public-key` names a key the administrator keeps, it SHALL generate the provisioner's key pair into the laptop's store as `MCP_PROVISIONER_PRIVATE_KEY` once, fill its public half into the SQL, and name `secret set MCP_PROVISIONER_PRIVATE_KEY --env <env> --from-local` as the command that copies it to an environment.

On Snowflake, over an existing warehouse or one the platform builds, with fixed names or a shared account, the SQL SHALL select the environment's warehouse before its first statement, so that it runs in a session with no default warehouse. Its query of the users and roles the `provisioner` owns SHALL print each one's `DROP` under the column `drop only after a destroy without a state restore`, because on a running environment, or one whose `state` snapshot was restored, they are the MCP's own.

#### Scenario: Script for Postgres

- **WHEN** `vecten mcp provisioner-script` runs in an MCP-only Postgres project
- **THEN** the output is SQL that creates `analyst`, `provisioner` and `impersonator` with exactly the contract's grants, sets the default privileges on the modelled schemas and the log prefix, states the `REVOKE CONNECT`s, and is followed by the two `secret set` commands

#### Scenario: Confirming the run

- **WHEN** `vecten mcp provisioner-script --confirm-run` runs after the administrator ran the script
- **THEN** it records the run and prints only that confirmation and the commands that follow, not the SQL

#### Scenario: Script for Snowflake

- **WHEN** it runs in an MCP-only Snowflake project after `init --deploy`
- **THEN** the output creates `analyst` and `provisioner` and includes `CREATE SECURITY INTEGRATION` for External OAuth with the deployment's issuer, JWKS URL and audience filled in
- **AND** the provisioner's public key in the SQL is the public half of the `MCP_PROVISIONER_PRIVATE_KEY` the run generated locally, and a second run keeps that key

#### Scenario: Session without a default warehouse

- **WHEN** an ACCOUNTADMIN whose session has no warehouse runs the Snowflake script of `prod` for project `acme`, or of `dev` in a shared account
- **THEN** the script's first statement is `USE WAREHOUSE "ACME_PROD_WH"`, or `USE WAREHOUSE "ACME_DEV_WH"`, and every `SHOW … RESULT_SCAN` check runs after it

#### Scenario: Rerun on a running environment

- **WHEN** the Snowflake script is rerun on a running environment whose `PROVISIONER` owns the group roles `FINANCE` and `SALES`
- **THEN** its last query prints `DROP ROLE "FINANCE";` and `DROP ROLE "SALES";` under the column `drop only after a destroy without a state restore`, and the comment above it says to keep them
