# mcp/warehouse-identity Specification

## Purpose
Every query runs as a warehouse user that belongs to the principal asking, one per human and one per service, so warehouse policies and the server's audit name the same identity. The server holds no per-principal warehouse secret: on Postgres it impersonates the principal's role from one impersonator login whose membership in that role exists only while the principal has a session, on Snowflake it mints a token for the principal from its own OAuth server.

## Requirements

### Requirement: Every query runs as the principal's own warehouse user
Every warehouse query issued by a tool call SHALL run as a warehouse user that belongs to the principal making the call. The server MUST NOT run any query through a shared identity or through the provisioner.

#### Scenario: Warehouse enforces as the person
- **WHEN** a person runs `sql-exec` or `query_metrics`
- **THEN** the warehouse checks every privilege, row policy and mask against that person's warehouse user
- **AND** the server's audit row for the call names the same person

#### Scenario: Snowflake query history names the person
- **WHEN** a person runs a tool call on a Snowflake deployment
- **THEN** Snowflake's query history attributes the statement to the person's user

#### Scenario: Postgres logs carry the person
- **WHEN** a person runs a tool call on a Postgres deployment
- **THEN** `current_user` for the statement is the person's role and `application_name` is the person's warehouse name
- **AND** the deployment's Postgres `log_line_prefix` includes `%a`, so each log line carries the person beside the session user
- **AND** the documentation states that the session user in Postgres logs is the impersonator role

#### Scenario: Person has no warehouse user yet
- **WHEN** a person whose warehouse user has not been provisioned calls a tool that needs the warehouse
- **THEN** the call fails with an error saying the person is not provisioned
- **AND** no query is run under any other identity

### Requirement: One warehouse identity per principal, adopted if it exists
A human principal SHALL have exactly one warehouse user, named as the identity contract derives it. When an admin creates a person, the server SHALL look the name up first and create the user when none exists.

#### Scenario: New person
- **WHEN** an admin creates Jane Doe and no `jane_doe` exists in the warehouse
- **THEN** a warehouse user `jane_doe` is created with no credential set by the server

### Requirement: Adoption is confirmed, bounded and credential-free
When the warehouse user already exists and is bound to no other principal, the panel or CLI SHALL show that it exists with its roles and last login and SHALL adopt it only after confirmation. When it is bound to another principal the request MUST be refused. Adoption MUST NOT set, replace or read any credential the person already uses.

#### Scenario: Existing account adopted
- **WHEN** an admin creates Jane Doe and `jane_doe` already exists unbound
- **THEN** the admin sees the account's roles and last login and confirms
- **AND** on confirmation the server records the identity as adopted, records the role in the provisioner registry, and her existing passwords and keys are untouched

#### Scenario: Adopting a Postgres role the provisioner did not create
- **WHEN** the existing `jane_doe` role on Postgres was not created by the provisioner
- **THEN** the panel prints `GRANT jane_doe TO provisioner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE` for an admin to run, so the provisioner can grant and revoke the per-session membership without inheriting her privileges
- **AND** adoption completes once the grant is in place

### Requirement: Postgres identity is a role per person with no server-set password
On a Postgres warehouse the server SHALL provision one role per person, named `<firstname>_<lastname>`, and SHALL set no password on it.

#### Scenario: Provisioning a person on Postgres
- **WHEN** an admin provisions Jane Doe on a Postgres deployment
- **THEN** a role `jane_doe` exists in the warehouse with no password
- **AND** `impersonator` holds no membership in `jane_doe` until she opens a session

### Requirement: Postgres queries run through the impersonator role
The server SHALL connect to Postgres as one login role, `impersonator`, and SHALL issue `SET ROLE <name>` and set `application_name` to `<name>` before any statement runs for a principal. The `impersonator` role MUST hold no `CREATEROLE`, `SUPERUSER`, `BYPASSRLS` or direct table privilege, and MUST hold membership in a principal's role only as the next requirement grants it.

#### Scenario: Query runs as the person
- **WHEN** Jane runs `sql-exec`
- **THEN** the connection has executed `SET ROLE jane_doe` before the statement
- **AND** a table `jane_doe` cannot read is refused by the warehouse

#### Scenario: Impersonator cannot read on its own
- **WHEN** a statement is issued on an impersonator connection without `SET ROLE`
- **THEN** the warehouse refuses every table in the warehouse database

### Requirement: Impersonator membership is scoped to the session
When a principal's session opens on Postgres, the server SHALL use the provisioning credential to run `GRANT <name> TO impersonator WITH SET TRUE, INHERIT FALSE`, and when the session ends, is revoked, or has been idle past the session idle timeout, it SHALL run `REVOKE <name> FROM impersonator`. At startup the server SHALL revoke every membership `impersonator` holds that has no open session.

#### Scenario: Session opens and closes
- **WHEN** Jane opens a session and later it ends
- **THEN** `impersonator` is a member of `jane_doe` only between those two moments
- **AND** `SET ROLE jane_doe` on an impersonator connection fails outside them

#### Scenario: Impersonator credential stolen
- **WHEN** someone connects as `impersonator` from outside the server while three people have open sessions
- **THEN** `SET ROLE` succeeds for those three roles only
- **AND** every other principal's role is unreachable

#### Scenario: Server restarts after a crash
- **WHEN** the server starts and `impersonator` still holds memberships from sessions that no longer exist
- **THEN** those memberships are revoked before the server serves traffic

### Requirement: Snowflake identity is a person-type user with no server-set credential
On a Snowflake warehouse the server SHALL provision one user per person of `TYPE = PERSON`, named `<firstname>_<lastname>`. The server MUST NOT set a password or key pair on the user, and keys or passwords the person already uses MUST be left in place.

#### Scenario: Provisioning a person on Snowflake
- **WHEN** an admin provisions Jane Doe on a Snowflake deployment
- **THEN** a user `jane_doe` of `TYPE = PERSON` exists with no credential set by the server
- **AND** Snowflake query history attributes her queries to `jane_doe`

### Requirement: Snowflake queries authenticate with a token from the server's own OAuth server
The server SHALL authenticate to Snowflake as a principal with a short-lived access token minted by its own OAuth authorization server, registered in the account as an External OAuth security integration of type `CUSTOM` with the issuer and JWKS URL on the MCP hostname and the token's user claim mapped to the Snowflake login name.

#### Scenario: Token per session
- **WHEN** Jane's session opens a warehouse connection
- **THEN** the server mints an access token for `jane_doe` scoped to her group role with a lifetime no longer than the session
- **AND** the connection authenticates with that token and nothing else

#### Scenario: Person already uses a key pair
- **WHEN** `jane_doe` already has a key pair she uses from her own tools
- **THEN** the server adds nothing to the user
- **AND** her existing key keeps working

#### Scenario: Existing warehouse
- **WHEN** the deployment is MCP-only over a Snowflake account the bootstrap did not create
- **THEN** `vecten mcp` prints the `CREATE SECURITY INTEGRATION` statement for the admin to run with the deployment's issuer and JWKS URL filled in

### Requirement: Service principals get their own warehouse identity
Each service principal SHALL have one warehouse user named `svc_<name>`, reached the same way as a person's: impersonated on Postgres, a `TYPE = SERVICE` user with a server-minted token on Snowflake. It MUST NOT share a warehouse user with any person or other service.

#### Scenario: Service principal provisioned
- **WHEN** an admin creates the service principal `reporting`
- **THEN** a warehouse user `svc_reporting` exists with no credential set by the server
- **AND** its queries run as `svc_reporting`

### Requirement: The server never creates or holds an admin warehouse account
`admin_<firstname>` accounts are created by the bootstrap over the master credential for direct warehouse work and are not principals. The server MUST NOT create, adopt or store a credential for any account whose name starts with `admin_`, and the panel SHALL offer no such action.

#### Scenario: Admin name refused
- **WHEN** an admin attempts to provision a principal whose warehouse name would start with `admin_`
- **THEN** the request is refused with a message that admin accounts are created by the bootstrap

#### Scenario: Panel has no admin account action
- **WHEN** the users screen is inspected
- **THEN** no action creates, adopts or rotates an `admin_` account

### Requirement: Connections are pooled per principal and never shared
The server SHALL keep a small connection pool per principal with an idle timeout. A connection opened for one principal MUST NOT serve a query for another: on Postgres a pooled connection carries `SET ROLE` for exactly one principal for its lifetime, on Snowflake it carries exactly one principal's token.

#### Scenario: Two people query concurrently
- **WHEN** two people run tool calls at the same time
- **THEN** each query runs on a connection bound to its own person
- **AND** an idle pool is closed after the idle timeout

### Requirement: No per-principal warehouse secret exists
The server SHALL hold no password, key or long-lived token for any principal's warehouse user. The only warehouse secrets the server holds are the `provisioner` credential, on Postgres the `impersonator` credential, and the signing key of its own OAuth server, each stored in the environment's secrets store under `mcp/`. None of them MUST be stored in `mcp_state`, logged, or included in any tool response or panel page.

#### Scenario: Inspecting the secrets store
- **WHEN** the secrets store is listed after principals have been provisioned
- **THEN** it holds no entry per principal

#### Scenario: Inspecting the state database
- **WHEN** `mcp_state` is read directly after principals have been provisioned
- **THEN** it contains no warehouse password, key or token material

### Requirement: Provisioning credential is held permanently and scoped tightly
The server SHALL hold one provisioning credential for the `provisioner` role, stored in the secrets store. Its permitted grants are the identity contract's set, where the roles it may hold `ADMIN` on are those in the provisioner registry in `mcp_state`, which records every role the provisioner created or an admin granted it for adoption. Over an existing warehouse the admin creates it from the script `vecten mcp provisioner-script` prints.

#### Scenario: Provisioner grants the per-session membership
- **WHEN** a person's session opens on Postgres
- **THEN** the provisioner grants `impersonator` membership in the person's role using the `ADMIN OPTION` it holds on every role it created or was granted

#### Scenario: Provisioning credential in use
- **WHEN** the server creates, adopts, disables or deletes a principal's warehouse user
- **THEN** it does so with the provisioning credential and no human step

#### Scenario: Impersonation is never done with the provisioning credential
- **WHEN** a principal's query is issued on Postgres
- **THEN** the connection was opened as `impersonator`, never as `provisioner`

#### Scenario: Existing warehouse
- **WHEN** the deployment is MCP-only over a warehouse the bootstrap did not create
- **THEN** `vecten mcp provisioner-script` prints the script that creates `provisioner`, `analyst` and `impersonator` with exactly these grants for the admin to run

### Requirement: Startup check on provisioning credential grants
At startup the server SHALL compare the provisioning credential's grants with the identity contract's set and the provisioner registry, and on Postgres verify that `impersonator` holds no attribute or privilege beyond `LOGIN` and its `SET TRUE, INHERIT FALSE` memberships, and MUST refuse to serve traffic on any difference.

#### Scenario: Over-privileged provisioning credential
- **WHEN** the provisioning credential is found at startup to hold a grant beyond its permitted set, such as `SUPERUSER` on Postgres
- **THEN** the server fails to start
- **AND** the failure names the offending grant

#### Scenario: Inheriting impersonator
- **WHEN** `impersonator` is found at startup to hold a membership with `INHERIT TRUE` or a direct table privilege
- **THEN** the server fails to start
- **AND** the failure names the membership or privilege

### Requirement: Admin lifecycle of warehouse users
An admin SHALL be able to create, disable and delete a principal's warehouse user from the admin panel or `vecten user`, both over the admin API. Disabling SHALL stop the server acting as the principal and MUST NOT touch a credential the person uses from their own tools; deleting SHALL drop the warehouse user only when the server created it. The server SHALL rotate the `impersonator` credential and its OAuth signing key on a schedule without interrupting a session in progress.

#### Scenario: Disabling a person
- **WHEN** an admin disables a person
- **THEN** the person's sessions and every access and refresh token issued to the person are revoked
- **AND** the person's pooled connections are closed and their open warehouse sessions are terminated
- **AND** on Postgres `impersonator` loses membership in the person's role at once, on Snowflake the server mints no further token for the user
- **AND** the person's next tool call fails with an access error
- **AND** a password or key the person set themselves keeps working outside the server

#### Scenario: Deleting an adopted identity
- **WHEN** an admin deletes a principal whose warehouse user was adopted rather than created
- **THEN** the principal is removed and the group roles the server granted are revoked
- **AND** the warehouse user itself is not dropped, because the server did not create it

#### Scenario: Scheduled rotation
- **WHEN** the rotation schedule fires
- **THEN** the `impersonator` credential is replaced in both the warehouse and the secrets store, and a new signing key is published at the JWKS URL beside the old one until every token signed with the old one has expired
- **AND** later queries succeed
