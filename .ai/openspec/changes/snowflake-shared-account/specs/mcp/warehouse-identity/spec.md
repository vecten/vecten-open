## MODIFIED Requirements

### Requirement: Adoption is confirmed, bounded and credential-free
When the warehouse user already exists and is bound to no other principal, the panel or CLI SHALL show that it exists with its roles and last login and SHALL adopt it only after confirmation. When it is bound to another principal the request MUST be refused. Adoption MUST NOT set, replace or read any credential the person already uses. On a Snowflake environment with a shared account the server MUST NOT adopt: an existing user of the derived name SHALL be refused, naming the user, because binding it to the environment's security integration would change how its owner signs in.

#### Scenario: Existing account adopted
- **WHEN** an admin creates Jane Doe and `jane_doe` already exists unbound
- **THEN** the admin sees the account's roles and last login and confirms
- **AND** on confirmation the server records the identity as adopted, records the role in the provisioner registry, and her existing passwords and keys are untouched

#### Scenario: Adopting a Postgres role the provisioner did not create
- **WHEN** the existing `jane_doe` role on Postgres was not created by the provisioner
- **THEN** the panel prints `GRANT jane_doe TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE` for an admin to run, so the provisioner can grant and revoke the per-session membership without inheriting her privileges
- **AND** adoption completes once the grant is in place

#### Scenario: Existing user in a shared account
- **WHEN** an admin creates Jane Doe in environment `dev` with a shared account and the user `JANE_DOE_ACME_DEV` already exists
- **THEN** the request is refused naming `JANE_DOE_ACME_DEV`, no adoption is offered, and the user is unchanged

### Requirement: Snowflake identity is a person-type user with no server-set credential
On a Snowflake warehouse the server SHALL provision one user per person of `TYPE = PERSON`, named `<firstname>_<lastname>`, followed by the environment's suffix when the account is shared. The server MUST NOT set a password or key pair on the user, and keys or passwords the person already uses MUST be left in place. With a shared account the server SHALL set the environment's authentication policy on every user it creates, so that the user signs in only by OAuth through the environment's own security integration.

#### Scenario: Provisioning a person on Snowflake
- **WHEN** an admin provisions Jane Doe on a Snowflake deployment with fixed names
- **THEN** a user `jane_doe` of `TYPE = PERSON` exists with no credential set by the server
- **AND** Snowflake query history attributes her queries to `jane_doe`

#### Scenario: Provisioning a person in a shared account
- **WHEN** an admin provisions Jane Doe in environment `dev` with a shared account
- **THEN** a user `JANE_DOE_ACME_DEV` of `TYPE = PERSON` exists with no credential set by the server and with the authentication policy `ACME_DEV.VECTEN_ACCESS.MCP_SIGN_IN`

#### Scenario: Another environment's signing key
- **WHEN** a token signed with `prod`'s signing key names the user `JANE_DOE_ACME_DEV`
- **THEN** Snowflake refuses the sign-in, because the user's policy names only `EXTERNAL_OAUTH_ACME_DEV`

### Requirement: Snowflake queries authenticate with a token from the server's own OAuth server
The server SHALL authenticate to Snowflake as a principal with a short-lived access token minted by its own OAuth authorization server, registered in the environment's account as an External OAuth security integration of type `CUSTOM`, named as the `shared/deploy` spec's "One source for Snowflake names" derives it, with the issuer and JWKS URL on the environment's MCP hostname and the token's user claim mapped to the Snowflake login name. Each environment SHALL have its own integration; with a shared account the administrator's script SHALL create and alter only the environment's own integration.

#### Scenario: Token per session
- **WHEN** Jane's session opens a warehouse connection
- **THEN** the server mints an access token for her Snowflake user scoped to her group role with a lifetime no longer than the session
- **AND** the connection authenticates with that token and nothing else

#### Scenario: Person already uses a key pair
- **WHEN** `jane_doe` already has a key pair she uses from her own tools
- **THEN** the server adds nothing to the user
- **AND** her existing key keeps working

#### Scenario: Existing warehouse
- **WHEN** the deployment is MCP-only over a Snowflake account the bootstrap did not create
- **THEN** `vecten mcp` prints the `CREATE SECURITY INTEGRATION` statement for the admin to run with the deployment's issuer and JWKS URL filled in

#### Scenario: Two environments in one account
- **WHEN** the provisioner scripts of `dev` and `prod` have both run in one shared account
- **THEN** `EXTERNAL_OAUTH_ACME_DEV` names `https://mcp.<dev domain>` and `EXTERNAL_OAUTH_ACME_PROD` names `https://mcp.<prod domain>`, and running `dev`'s script again leaves `prod`'s integration unchanged
