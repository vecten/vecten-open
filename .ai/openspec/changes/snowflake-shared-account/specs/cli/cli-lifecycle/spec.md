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
