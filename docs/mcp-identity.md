# MCP identity and access

This guide covers authentication and warehouse identities for Vecten's Model Context Protocol
(MCP) server. It is for administrators running a generated Vecten project. Commands below run
from that project's directory, which contains `vecten.toml`.

## What the server does

The server supports authenticated streamable HTTP at `/mcp`, password and OpenID Connect (OIDC)
login, browser consent, device login, service credentials, principal administration, and
Postgres connections under each principal's warehouse role. A principal is a person or service
record in `mcp_state`, the server's private PostgreSQL database. The HTTP admin API and CLI use
the same identity and access services. No admin mutation is exposed as an MCP tool.

The `provisioner` is the warehouse login used by MCP to create
principals and manage their role memberships and data grants. Its allowed privileges are
specified in the [identity contract](../.ai/openspec/specs/shared/identity-contract/spec.md).
The `loader` is the role that builds and owns tables in a Vecten-managed warehouse; an existing
warehouse may use different object owners.

PostgreSQL setup installs restricted functions that grant and revoke read access on behalf of
schema and table owners. A schema grant covers existing tables/views and future tables made
by the registered creator roles. The provisioner can call these functions but cannot become
an owner or edit their registry. Dataset grants add only SELECT on the named table and the
schema USAGE needed to reach it. Setup follows PostgreSQL's [security-definer guidance](https://www.postgresql.org/docs/16/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY).

Rerun administrator setup after adding a schema or object-creator role. Reconciliation reports
missing helper coverage before changing grants; it does not claim future-table access for an
unregistered creator. Setup preserves existing owner permissions and removes its temporary
grants. This installer is included in both managed PostgreSQL bootstrap and the printed
existing-warehouse script.

Snowflake External OAuth (issuer, audience, role scope, query identity and key rotation) is
exercised against a real account by the `snowflake_live` test tier, which the maintainers run
before a release; unit tests and generated SQL are not that verification.
The generated Snowflake setup SQL grants `analyst` database and compute warehouse usage only,
plus the provisioner's table SELECT grant options on core/marts. It installs an owner-rights
procedure in the configured database's `VECTEN_ACCESS` schema. The procedure accepts only
`ANALYST` or provisioner-owned reader roles, and the convention schemas, `RAW_<SOURCE>`
schemas and schemas matching `^(CORE|MARTS)_[A-Z][A-Z0-9]*$` (restricted areas, matched by
pattern, so declaring an area needs no rerun), and can grant/revoke only read privileges.

A schema or area grant covers tables, views, materialized views, dynamic tables, external
tables and Iceberg tables, each with its current and future grants. On a grant, a kind the
account refuses is listed in access health with Snowflake's message while the others apply;
on a revoke, a refusal fails the call while the role still reads anything in the schema, and
the revoke is retried on every reconciliation. A dataset grant takes its relation's object
type from `INFORMATION_SCHEMA.TABLES`, trying a base table as a table, a dynamic table and an
Iceberg table in turn, and counts a statement only once `SHOW GRANTS` reads the change back.
On Postgres the helpers cover tables, partitioned tables, views, materialized views and
foreign tables.

Run setup under an administrator authorized to manage existing and future grants; that
authority is kept on the procedure owner, never granted to provisioner.
See Snowflake's [owner-rights procedure rules](https://docs.snowflake.com/en/developer-guide/stored-procedure/stored-procedures-rights)
and [grant authority requirements](https://docs.snowflake.com/en/sql-reference/sql/grant-privilege#access-control-requirements).

A person in one group gets a token scoped to that group's role (`session:role:<ROLE>`), which
Snowflake accepts for no other role, and the session runs under that role alone. A person in
several groups needs `USE SECONDARY ROLES`, which Snowflake allows in an External OAuth session
only under the scope `session:role-any`, with `EXTERNAL_OAUTH_ANY_ROLE_MODE` enabled, which
setup sets. That connection signs in with one group role as its primary role and then activates
exactly the group roles as secondary roles, so the user's default role and default secondary
roles never apply; failure to activate them closes the connection. Such a token is accepted for
any role the Snowflake user holds, so for an adopted user with broader roles it is wider than the
session it opens; it never leaves the server and lives at most an hour. The `snowflake_live` tier covers this path,
group unions included.

Authenticated tool calls produce [audit records and receipts](mcp-audit.md). The built-in
query, discovery and feedback tools are in the [tool reference](mcp-tools.md), and the
[admin panel](admin-panel.md) manages the same identities, groups and grants from a browser.
Installed tool extensions use the same transport and its audited query execution service.

## First administrator and login

On the laptop, the default is password login. In a cloud environment, the default is OIDC;
missing OIDC settings refuse login. Set `[mcp] oidc_issuer` and `oidc_client_id` in `vecten.toml`
and, when required by the identity provider, store `MCP_OIDC_CLIENT_SECRET` using
`vecten secret set MCP_OIDC_CLIENT_SECRET --env <environment>`. Register
`https://mcp.<your-domain>/oidc/callback` at the identity provider. An explicit
`[mcp] auth = "password"` enables password login in a cloud environment.

Add administrator email addresses to `[mcp] admins`. A matching verified first login seeds the
principal's admin flag. This flag controls the MCP admin API; it never grants warehouse
administration rights. With no enabled human administrator, the admin API refuses operations.

For initial password setup in a cloud environment, run `vecten mcp admin-link --env <environment>`
(add `--email <admin>` for an address other than the first in `[mcp] admins`). It stores a
one-time token as `MCP_ADMIN_LINK` with your cloud credentials and prints the `/set-password`
page and the token to enter there; the token is never part of a URL. The server reads that
secret when the token is used, so no restart is needed; it works once, within 15 minutes, for
an address still in `[mcp] admins`. Run the command again to issue a newer token, which
replaces the older one, or to reset a lost admin password.

On the laptop, store a randomly generated token with at least 32 bytes of entropy using
`vecten secret set MCP_ADMIN_SETUP_TOKEN`. Enter its value at the hidden prompt; do not put it
in shell arguments. At startup, the server creates a 15-minute, single-use setup link for the
**first** email in `[mcp] admins`, if that email has no password. Visit `/set-password` on the
MCP hostname and enter that token and a password of at least 12 characters. Remove the
bootstrap token from `.env` after setup. Subsequent
users receive setup tokens through `vecten user password-link <principal-id> --env local`; use `--help` for options.
Find the principal ID with `vecten user list --env local` (or your cloud environment).
Setting a password through a link signs the person out everywhere: it revokes their client
sessions and browser sessions and voids any other link issued for them. Logins and
set-password attempts are limited per person and per client address.
After changing local environment secrets, run `vecten down` followed by `vecten up` to recreate
the local services with the updated environment.

Run `vecten login --env local` for device login: open the displayed verification page, enter
the code, sign in, and approve the client. Use `--browser` for a browser opened from the same
computer with a loopback callback. For a headless workflow, `--json` starts the device flow;
complete the browser step and run `vecten login --env local --complete --json`.

`vecten logout --env local` revokes the cached session. The cache lives at
`$XDG_CONFIG_HOME/vecten/tokens.json`, or `~/.config/vecten/tokens.json`, with owner-only
permissions. It stays outside the project. Access tokens last at most one hour; sessions last
at most seven days. Refresh tokens rotate on use, and detected reuse revokes their session.
`vecten login` records which session it saved for the project in
`.vecten/records/login/<env>.json`, holding the client id and no token, and `vecten next`
counts a session only when that record names it. A session the MCP rejects is dropped from
the cache.

Connect MCP clients to `https://mcp.localhost:8443/mcp` locally, over Postgres; on Snowflake
the local stack has no MCP, because Snowflake's External OAuth trusts only the deployed
issuer. For cloud, run
`vecten mcp connection --env <environment>` to print the exact endpoint. The legacy `/sse`
transport is unavailable. Use the connected-clients page at `/connected-clients` to review and
revoke the browser user's client sessions.

## People, services, and warehouse roles

`vecten user add --email jane@example.com --name 'Jane Doe' --env local` invites a person and
provisions a warehouse identity. Names become lowercase ASCII identifiers with collision
suffixes; `--warehouse-name` supplies an explicit permitted identifier. `admin_` names are
reserved for warehouse administrators, and `svc_` names identify services. A newly seeded
MCP administrator can be provisioned with the same `user add` command using their email.

If the proposed warehouse account already exists, the API reports its roles, last login when
the warehouse provides it, and any adoption SQL. Review this information before repeating the
command with `--adopt`. Postgres adoption requires the warehouse administrator to grant the
provisioner `ADMIN TRUE, INHERIT FALSE, SET FALSE` on that account's role. Adoption never
changes the person's password or key. Deletion preserves an adopted warehouse account;
deletion drops an account created by Vecten. Disabling blocks MCP access without changing a
person's credentials for their own warehouse tools.

Services use `vecten service add` and OAuth client credentials. The CLI shows the newly created
client secret once. Store it in the consuming application's secret store. People cannot use
this grant, and services cannot receive MCP admin rights. Vecten creates no password or key on
individual warehouse accounts: Postgres uses a NOLOGIN role; Snowflake uses a PERSON or
SERVICE user authenticated through External OAuth.

### Group roles and bound roles

Each group maps to a warehouse role the provisioner creates and owns, named after the group
unless `vecten group add finance --role fin_readers` (or the panel's group form) names it. A
role name follows the warehouse-name rules and may not start with `admin_`, `svc_`, `pg_` or
`p_` (kept for one reader role per person). When a role of that name already exists, creation
is refused and names both ways out: another role name, or binding the existing role.

A group can read through a role the client already has, such as `FINANCE_ANALYST` with its
own grants and row access policies, by binding it:

1. `vecten group bind finance finance_analyst` records the binding as pending and prints two
   statements for an administrator. The review lists every grant the role holds, through
   every role nested under it: on Snowflake a recursive query over
   `SNOWFLAKE.ACCOUNT_USAGE.GRANTS_TO_ROLES`, which lags by up to two hours. The grant nests
   the role under the group's role: on Snowflake `GRANT ROLE "FINANCE_ANALYST" TO ROLE
   "FINANCE";`, on Postgres `GRANT "finance_analyst" TO "finance" WITH INHERIT TRUE, SET
   FALSE;`.
2. An administrator reviews the role and runs the grant.
3. `vecten group bind finance finance_analyst --confirm` checks that the group's role holds
   the bound role (`SHOW GRANTS TO ROLE` on Snowflake, `pg_auth_members` with inherit on
   Postgres) and records it. Binding a confirmed role again prints its grant and keeps the
   record.

`vecten group unbind finance` forgets the binding and prints the revoke for an administrator
to run. Each reconciliation reads the roles granted to every group's role. Access health and
`vecten issues` list a confirmed binding whose grant is gone, and every other role nested
under a group's role, including an unbound one until its revoke has run.

The server never owns, grants, alters or drops a bound role, and never targets it with a
dataset, schema or area grant or with drift revocation: `READ_GRANT` and the Postgres helpers
refuse it as a target, and deleting the group drops only the group's own role. The default
group, system roles (`ACCOUNTADMIN`, `SYSADMIN`, `PUBLIC` and the like), Postgres's predefined
`pg_` roles, the roles setup creates, any role the server manages, and a role bound to another
group cannot be bound, nor can a group's role be named after a bound role. On Postgres, binding
and confirming also refuse a role that has, or is a member at any depth of a role that has,
`SUPERUSER`, `BYPASSRLS`, `CREATEROLE`, `CREATEDB` or `REPLICATION`, a predefined `pg_` role,
`rds_superuser`, `cloudsqlsuperuser`, `azure_pg_admin` or a managed role. Postgres binds a role
by its lower-case name. The provisioner cannot read a Snowflake role it does not own, so there
the review is the check.

The provisioner owns each group's role, and an owner can grant itself the roles it owns, so it
can reach whatever a bound role reaches. A bound role must therefore never hold account-level
privileges or roles such as `ACCOUNTADMIN`, `SECURITYADMIN`, `SYSADMIN` or `USERADMIN`. While a
binding exists, the MCP's startup check refuses `USAGE` on a group role held by `PROVISIONER`.
What a bound role reads, including outside the environment's database, is the client's to set,
and the warehouse enforces it.

A person's session runs under the group's role (or, with one reader role per person, their
own), never the bound role, so a policy keyed on `CURRENT_ROLE()` does not match a bound role.
On Snowflake, key row access and masking policies on `IS_ROLE_IN_SESSION('FINANCE_ANALYST')`,
which matches a role inherited through the role hierarchy. On Postgres, a row-level security
policy uses `pg_has_role(current_user, 'finance_analyst', 'USAGE')`, which follows inherited
memberships the same way.

## Access and enforcement

Groups map to warehouse roles. Every principal starts in the `default` group, mapped to
`analyst`. `core` and `marts` are literal schema names for modelled warehouse tables. Managed
warehouse setup creates them; existing warehouse setup uses the administrator-run SQL described
below. The MCP records the default group's grants on those two schemas once, at its first
start, and its reconciliation applies them; no deploy, ship or setup script grants `analyst`
anything on a schema while `mcp` is a component, so revoking `marts` from the default group
holds. Without the MCP, setup grants `analyst` the two schemas. Raw ingestion schemas,
`staging`, and `snapshots` have no default grant.

Reconciliation also reads the read grants `analyst` and every group role hold in the
convention and `raw_<source>` schemas and the schemas of declared areas (on Snowflake through
the owner-rights `VECTEN_ACCESS.READ_GRANTS` procedure, on Postgres from the catalogs) and
revokes those the state does not hold. The `reconcile_drift` setting, `revoke` by default, can
be set to `report` to list them in access health without revoking; an MCP state created by an
earlier release starts at `report`. A grant in any other schema, or to a role the MCP does not
manage, is left alone. While the installed procedures are older than the MCP, this step is
skipped, grants that reach an area are refused and wait, and access health names
`vecten mcp provisioner-script`. Memberships combine the permissions of all assigned groups. Removing a grant from one group does not remove access provided by another.

Use `vecten group`, `vecten grant`, `vecten revoke`, and `vecten reconcile` for group membership
and desired grants. A dataset grant names one dataset in the semantic model under `osi/`;
a schema grant covers an entire schema. This implementation accepts only the literal schemas
`core`, `marts`, `staging`, `snapshots`, names beginning with `raw_`, such as
`raw_hubspot`, and the schemas of an area `vecten.toml` declares; it rejects arbitrary other
schema names.

An area grant (`vecten grant --area finance --group finance`) is a schema grant on both
`core_finance` and `marts_finance`, applied and revoked together; one that fails on the second
schema is rolled back on the first. It is refused for an area with no `[areas.<area>]` entry,
and an area removed from `vecten.toml` has its grants revoked and forgotten, so declaring it
again starts with none. Each granted schema carries its own future or default grant, so a
relation dbt rebuilds there is readable by the granted groups and no group without the grant,
with no reconciliation in between. No group reads an area until it is granted, with these
exceptions, which the warehouse and not the MCP decides:

- a dataset grant on a mart in the area, which reaches that one relation;
- a future grant an administrator made for the whole database, which reaches every schema
  without a schema-level one; access health lists those it finds on managed roles;
- `loader`, which owns the area's schemas and builds them, and the roles above it: on
  Snowflake `SYSADMIN` and the deploy role, on Postgres the master that sets its role.

On Postgres the setup that `ship` runs creates every declared area's schemas as `loader` and
installs their helpers; over an existing warehouse, rerun the printed script once dbt has
created them.

Desired grants are reconciled every fifteen minutes, on a semantic-model update, and on demand. The default setup grants are recorded once,
so an administrator's later revocation is not undone merely by restarting MCP. See the
schema-grant limitations above before relying on these operations. Reconciliation
attempts independent grants even when another grant fails. Its Boolean `complete` field is
false whenever any operation fails; `reconciled` counts successful grants and `failures` lists
failed operations. The CLI exits with
code 5 for an incomplete result. A dataset revoke can finish while reconciliation of remaining
schema grants fails; its response reports both outcomes. Failed operations are retried on the
next reconciliation cycle.

Per-group column deny lists filter metadata in the [MCP discovery and metric tools](mcp-tools.md);
they are **not** a warehouse permission or a substitute for a view that excludes sensitive columns. Denials combine across
a principal's groups. The warehouse is the only enforcement point for query data access.

## Connections, credentials, and compromise

Postgres query connections authenticate as `impersonator`, then set `ROLE` and `application_name`
to the principal before a query. `current_user` is the principal; `session_user` remains
`impersonator`. Database logging should include application name to preserve the human identity.
The provisioner grants `impersonator` permission to SET a principal's role only while sessions
for that principal are active, and revokes it when the last session ends or expires. Startup
clears stale memberships. Since membership belongs to the warehouse login, another connection
holding the impersonator credential can SET any role with an active session; it cannot SET a
role whose membership is absent.

Connections are pooled per principal, with a default limit of two and a fifteen-minute idle
timeout. Revocation closes the affected connections. Each acquisition rechecks persistent
session and principal state. A user's connections are never lent to a different principal.

The provisioner credential and OAuth signing key are standing power available to an attacker
who compromises the MCP process. The server does not store per-person warehouse credentials.
Passwords are hashed with argon2id; bearer tokens, setup/device codes, and service client
secrets are stored as hashes. Private signing keys remain in the secret provider: the local private Docker volume, AWS
Secrets Manager, or Scaleway Secret Manager. Cloud credentials restrict access to this project's
environment and component secrets.

In a deployed environment with `mcp`, the MCP VM is also the setup host: its cloud identity
reads the database instances' master credentials to run the setup SQL. The MCP container can
use that identity, through the instance metadata service on AWS and through the VM's API key in
its environment on Scaleway. Code that runs inside the MCP container can therefore act as the
database administrator, which includes changing or deleting audit records. Under the `minimal`
profile, DAG code runs on the same VM and has the same reach. Treat a compromise of the MCP
process, or of DAG code under `minimal`, as a compromise of the databases.

On a deployed environment the MCP server and Airflow require TLS on their Postgres
connections. The loader and dbt use TLS whenever the server offers it, as both clouds' managed
Postgres does. None of these connections verifies the server's certificate: they rely on the
private network for the database's identity, so a party that can redirect traffic inside that
network can impersonate the database.

Signing keys and the Postgres impersonator password rotate every thirty days. Old public
signing keys remain in `/jwks` for one hour after rotation so issued tokens can expire. Password
rotation leaves existing connections open. During an interrupted rotation, the impersonator
secret may temporarily contain a JSON object with `current` and `pending` values; both are
login candidates until a successful retry replaces it with one password. Never copy that object
into a password field manually. The VM setup agent, the deployment process that initializes databases on each cloud virtual
machine, understands this recovery format when reapplying setup. MCP maintenance retries failed
rotations every thirty seconds once a rotation is due.

Locally, rotated secrets persist in the private `mcp-secrets` Docker volume. Deleting that volume
loses signing history and may lose the current impersonator password; retain it with the
Postgres data volume. Cloud runtime writes are restricted to `MCP_SIGNING_KEY` and `MCP_IMPERSONATOR_PASSWORD`. These names are enforced by the runtime provider and deployment
permissions.

## Existing warehouse setup

This procedure applies when `components = ["mcp"]` and `[warehouse] existing = true` in
`vecten.toml`: MCP connects to an existing warehouse without running Vecten's ingestion platform.
Run `vecten mcp provisioner-script` to review SQL for a warehouse administrator. On Snowflake,
first configure the cloud deployment settings using `vecten init --deploy`, as described in the
[AWS deployment guide](deploy-aws.md) or
[Scaleway deployment guide](deploy-scaleway.md). The environment's domain, the `domain` in
`deploy/envs/<env>.tfvars` (for `prod`, `[deploy] domain`), determines the public HTTPS issuer and
JWKS URL. On its first run the script generates the provisioner's key pair into the
laptop's store as `MCP_PROVISIONER_PRIVATE_KEY`, kept on one line because the local `.env`
holds no newline, and fills the public half into the SQL; the private key never reaches the
locator or the SQL. `vecten secret set MCP_PROVISIONER_PRIVATE_KEY --env <env> --from-local`
copies it to the environment. An administrator who keeps the private key elsewhere passes the
public key with `--public-key` instead, and nothing is generated. Postgres SQL reads the
provisioner and impersonator passwords from the administrator's environment with psql 15 or
later, whose `\getenv` older versions lack.
The Snowflake output expects the CORE and MARTS schemas and the compute warehouse
`<PROJECT>_<ENV>_WH` to exist, and creates neither schemas nor compute resources. Its
administrator is ACCOUNTADMIN or a role holding MANAGE GRANTS: in a regular schema a future
grant needs that privilege even from the schema's owner.

Printing the script does not execute it or mark it successful. After the administrator has
run it, acknowledge that result with `vecten mcp provisioner-script --confirm-run` using the same
`--env` value. The output ends with the `vecten secret set` commands needed by MCP, as SQL
comments, so the saved output runs as is with `psql -f`.

## A Snowflake warehouse the platform builds

With the platform and the MCP on Snowflake, the platform's Terraform root creates the database,
the schemas, the warehouse and the roles, but not the MCP's identity: the `VECTEN_PROVISIONER`
user, its account grants, the access procedures and the `VECTEN_EXTERNAL_OAUTH` integration need
an ACCOUNTADMIN. The MCP checks its scope as `VECTEN_PROVISIONER` when it starts, so:

1. The first `vecten deploy --env <env>` applies the roots and ships no host.
2. `vecten mcp provisioner-script --env <env>` generates the provisioner's key pair into the
   environment's secrets store as `MCP_PROVISIONER_PRIVATE_KEY` on its first run, and prints the
   SQL with the public key filled in; it takes no `--public-key`. An ACCOUNTADMIN runs the SQL.
3. `vecten mcp provisioner-script --env <env> --confirm-run` records the run in `vecten.toml`,
   and `vecten github push` carries it to GitHub, where the ship workflow ships every host.

Until the run is recorded, `vecten ship` refuses and the ship workflow finishes without shipping.
`vecten next` reports each step.

### Several environments in one Snowflake account

With `shared_account = true` under `[warehouse]` in `vecten.toml`, environments of the project may
share one Snowflake account, and other teams' data may sit beside them. Each environment then has
its own users, roles and integration, named with the environment's suffix `_<BASE>_<ENV>`
(`<BASE>` is `[warehouse] database` or the project name, `<ENV>` the environment, both in upper
case): the provisioner `PROVISIONER_ACME_DEV` (role and user), the integration
`EXTERNAL_OAUTH_ACME_DEV`, the default group's role `ANALYST_ACME_DEV`, and for people, services
and groups `JANE_DOE_ACME_DEV`, `SVC_REPORTING_ACME_DEV` and `FINANCE_ACME_DEV`. The MCP state
keeps the names without the suffix (`jane_doe`, `finance`, `analyst`); the server adds the suffix
whenever it names a Snowflake user or role, and a grant never targets another environment's role.

- **Per-environment users.** A person added in `dev` and in `prod` has two Snowflake users,
  `JANE_DOE_ACME_DEV` and `JANE_DOE_ACME_PROD`, each holding only its environment's roles. Query
  history shows that name, not the person's own corporate user, so row access and masking
  policies keyed on a person's user do not apply to MCP queries; key them on roles.
- **Bound to one integration.** The administrator's script creates the authentication policy
  `<BASE>_<ENV>.VECTEN_ACCESS.MCP_SIGN_IN`, which admits OAuth through the environment's own
  integration only, and the MCP sets it on every user it creates. A token signed with another
  environment's key names a user that refuses it. The provisioner's user and the loader sign in
  by key pair only (`SERVICE_SIGN_IN`), and each integration refuses `SYSADMIN` and the role that
  created the environment's database.
- **No adoption.** Setting the policy on an existing user would change how its owner signs in, so
  in a shared account the MCP refuses a user that already exists, naming it.
- **Checks before change.** The provisioner script runs as one block that changes nothing when a
  role, user or integration of the environment's names carries another comment or owner or is
  held by a stranger, or when the access schema or a policy has another owner; the MCP's startup
  check refuses a stranger holding its provisioner or analyst role.
- **Destroy.** `vecten destroy` reads by ownership what the provisioner created and lists it as
  kept; with `--delete-data` it refuses while any of it stays and prints the statements an
  administrator runs.

What this protects, and what it does not: one environment's keys (its loader key, provisioner key
and MCP signing key) reach no other environment's data. It does not protect an environment from
someone who already controls another environment's provisioner, which holds account-level
`CREATE USER` and `CREATE ROLE` and can create objects named for another environment before that
environment's first script run. So create `prod` first, or give it an account of its own, put
every human deploy user on a key-pair-only authentication policy (the runbook gives the
statement), and give each environment its own deploy user. Several of these guarantees rest on
Snowflake behaviour that the project's live checks have not yet confirmed (several `CUSTOM`
integrations in one account, an authentication policy limiting External OAuth to one integration,
the provisioner setting that policy); until they pass, use a shared account for non-production
environments.

Introspection uses the existing-warehouse administrator connection to discover catalog objects
that an unprovisioned MCP identity cannot yet see, as the login `warehouse_url` names or `--user`
passes. On Postgres it signs in with the password stored locally using
`vecten secret set WAREHOUSE_ADMIN_PASSWORD`. On Snowflake, which gives a service user no
password and meets a person's password with MFA, it signs in with a key pair it generates into
the laptop's store as `WAREHOUSE_ADMIN_PRIVATE_KEY` on first use. Until Snowflake knows that key,
`vecten mcp introspect` exits 3 and prints the `ALTER USER <login> SET RSA_PUBLIC_KEY = '…'`
statement for an administrator to run; the login needs a default role that reads the schemas and
a default warehouse. Both credentials stay on the laptop. Then run
`vecten mcp introspect --schema analytics`. The command reads only
catalog metadata and drafts `osi/datasets.json`; it refuses to overwrite an existing file unless
`--replace` is supplied. Postgres defaults the login to `postgres`; Snowflake requires one.
Review the draft, remove unwanted tables, add descriptions, and run `vecten mcp validate`.
The warehouse locator contains host/account and database, never a password.

Back to the [docs index](README.md).
