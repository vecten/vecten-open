# shared/deploy Specification

## Purpose
Deployment of a project carrying the `platform` component, the `mcp` component or both to one of two clouds, AWS or Scaleway: the provider interface every cloud implements, the one-time bootstrap, the shape every environment has, the services each cloud maps it to, the Snowflake module, and how environments and CI are laid out.

## Requirements

### Requirement: Components and profile

An environment SHALL carry the components named by `components` in `vecten.toml`: `platform`, `mcp` or both. `profile` in `vecten.toml` SHALL be `standard`, the default, or `minimal`. Under `standard` each component runs on its own VM and the state databases on their own instance; under `minimal` one VM and one instance carry everything. The hostnames `airflow.<domain>` and `mcp.<domain>` SHALL be issued under every profile.

#### Scenario: Both components, standard

- **WHEN** `deploy --env prod` runs for a project with both components and the default profile
- **THEN** two VMs exist, one running the `platform` profile and one running the `mcp` profile, and two Postgres instances exist
- **AND** `airflow.<domain>` resolves to the first VM and `mcp.<domain>` to the second

#### Scenario: Both components, minimal

- **WHEN** `deploy --env prod` runs with `profile = "minimal"`
- **THEN** one VM runs both Compose profiles and one Postgres instance holds every database
- **AND** both hostnames resolve to it

#### Scenario: MCP only

- **WHEN** `deploy --env prod` runs for a project whose `components` is `["mcp"]`
- **THEN** one VM runs the `mcp` profile and no Airflow service exists
- **AND** `mcp.<domain>` resolves to it

### Requirement: Supported clouds

A cloud outside `aws` and `scaleway` SHALL be refused by name before any Terraform runs.

#### Scenario: Unsupported cloud

- **WHEN** `vecten.toml` names a cloud outside the two
- **THEN** the deploy exits with an error listing the supported clouds before any Terraform runs

### Requirement: Provider interface

Every provider SHALL offer the same eight operations: `bootstrap` an environment for CI, `provision` an environment and return its outputs, `destroy` it, `put_secret`, `get_secret`, `connection(env, db)` for `db` one of `warehouse`, `airflow` and `mcp_state`, `log_target` for an environment, and `ship(env, component, project_dir)` to place the content bundle on the component's host and start its profile. Providers SHALL exist for `aws`, `scaleway` and `local`.

#### Scenario: Contract across providers

- **WHEN** the provider contract tests run
- **THEN** all three providers pass the same test set with their cloud SDKs mocked

#### Scenario: Connection per database

- **WHEN** `connection` is asked for `airflow` and for `mcp_state` in the same environment
- **THEN** both point at the environment's `state` instance
- **AND** each names its own database and its own role

### Requirement: connection returns a typed specification

`connection` SHALL return a typed specification, kind, host, database, role or user, authentication method and, for Snowflake, account, role and warehouse, never a string.

#### Scenario: Snowflake connection

- **WHEN** `connection` is asked for `warehouse` on a Snowflake project
- **THEN** the specification carries the account, the role and the warehouse, and names External OAuth as the authentication method

#### Scenario: Unknown database name

- **WHEN** `connection` is asked for a `db` outside the three
- **THEN** the call fails naming the three allowed values

### Requirement: Local provider

The `local` provider SHALL read secrets from `.env`, SHALL return the local stack's Postgres for every `connection` database, SHALL name a local Docker volume as the log target, SHALL do nothing on `bootstrap` or `provision`, and SHALL treat `ship` as a no-op for every component.

#### Scenario: Secret on the laptop

- **WHEN** a DAG asks the provider for a secret while running under `up`
- **THEN** the value comes from `.env`

#### Scenario: Provision locally

- **WHEN** `provision` is called on the local provider
- **THEN** it returns without creating anything and without error

### Requirement: Runtime code uses only get_secret, connection and log_target

Code that runs inside DAGs, connectors and the MCP server SHALL use only the provider's `get_secret`, `connection` and `log_target` operations, with one exception: the MCP server SHALL call `put_secret` for `mcp/impersonator`, because it rotates that credential. `bootstrap`, `provision`, `destroy` and `ship`, and `put_secret` for any other name, SHALL be reachable only from the CLI.

#### Scenario: Runtime inspection

- **WHEN** rendered DAGs, connectors and the server image are inspected
- **THEN** none call `bootstrap`, `provision`, `destroy` or `ship`
- **AND** only the server image calls `put_secret`, and only for `mcp/impersonator`

### Requirement: Ship reaches the VM without SSH open to the internet

`ship(env, component, project_dir)` SHALL place the content bundle on the component's VM, build the project image layer there, run the in-database setup script when the component owns a database, and start that component's profile, without opening SSH to the internet: AWS through SSM Run Command; Scaleway, which offers no run-command API, by a pull. On Scaleway CI SHALL upload the bundle under `bundles/<host>/` in the environment's bucket and rewrite that host's pointer object to name its commit; a watcher that the VM's user data installs SHALL pull the bundle the pointer names, ship it with the same VM agent as on AWS, and write a status object with the outcome under the host's first component's prefix, which CI waits for, bounded by a window, before it polls the component's URL.

#### Scenario: Ship platform on AWS

- **WHEN** `ship` runs for the `platform` component of an AWS environment
- **THEN** the project is on that VM and the `platform` profile is running, with no inbound port other than 80 and 443 open
- **AND** the command returns only after `airflow.<domain>` answers

#### Scenario: Ship mcp on Scaleway

- **WHEN** `ship` runs for the `mcp` component of a Scaleway environment
- **THEN** the bundle is uploaded under `bundles/mcp/` in the environment's bucket and the `mcp` host's pointer names its commit, the VM's watcher pulls it, restarts the `mcp` profile and writes its status object
- **AND** the command returns only after the status object reports success and `mcp.<domain>` answers

#### Scenario: Scaleway watcher never answers

- **WHEN** `ship` runs for a Scaleway host whose watcher writes no status object within the window, because the VM is still booting or holds no key yet
- **THEN** `ship` exits non-zero naming the host, and never reports success

### Requirement: Ship keeps the previous bundle

Every `ship` implementation SHALL keep the previous bundle, poll the component's URL, and restore the previous bundle when the poll fails.

#### Scenario: Poll fails

- **WHEN** a shipped bundle leaves the component's URL unanswered within the poll window
- **THEN** the previous bundle is restored and `ship` exits non-zero naming the component

### Requirement: Environment shape

Every environment SHALL have: the Postgres instances the profile requires; one VM per host with its own cloud identity; a secrets store and the identities GitHub Actions deploys and plans with, both created by `bootstrap`, which are an OIDC trust on AWS and, on Scaleway, whose IAM offers no OIDC federation, scoped API keys stored as GitHub secrets; an object storage bucket with a prefix per component; DNS records for both hostnames with TLS on each VM; and one network with inbound 80 and 443 only.

#### Scenario: Deployed environment on any cloud

- **WHEN** `deploy --env prod` completes on either cloud
- **THEN** every listed item exists for `prod`
- **AND** the only inbound ports open to the internet are 80 and 443, with 80 serving only the certificate challenge and a redirect

#### Scenario: Snowflake warehouse

- **WHEN** the project's warehouse is Snowflake
- **THEN** the `state` instance is still provisioned and no `warehouse` instance exists

#### Scenario: Two hosts, two certificates

- **WHEN** both components are deployed under the `standard` profile
- **THEN** each VM holds a Let's Encrypt certificate for its own hostname

### Requirement: State and warehouse are separate instances

Under the `standard` profile a `state` instance of the cloud's state class, which "Sizing tiers" names, SHALL hold `airflow` and `mcp_state` and no human login role, and for Postgres projects a `warehouse` instance sized by tier SHALL hold `warehouse` with the `analyst`, `loader`, `provisioner`, `impersonator` and person roles. Under `minimal` one instance SHALL hold all three databases, and SHALL take the warehouse class and storage of the tier when it holds `warehouse`.

#### Scenario: Person's role tries the state database

- **WHEN** a role the `mcp` component created for a person attempts to connect to `airflow` or `mcp_state`
- **THEN** the connection is refused, under `standard` because the role does not exist on the `state` instance and under `minimal` because CONNECT is revoked

#### Scenario: Warehouse restored from a snapshot

- **WHEN** the `warehouse` instance is restored to an earlier point in time under `standard`
- **THEN** the audit in `mcp_state` is unaffected

### Requirement: Every database is isolated on its instance

On every instance each database SHALL be owned by its own role and CONNECT SHALL be revoked from PUBLIC, so that a person's role, or `impersonator` wearing it, can reach `warehouse` and nothing else.

#### Scenario: Owner roles

- **WHEN** the databases are inspected after deploy
- **THEN** each has a distinct owner role and PUBLIC holds no CONNECT privilege on any of them

### Requirement: Managed Postgres names the impersonated person in its log

When `mcp` is a component and the warehouse is Postgres, the setup script SHALL create the `impersonator` login role with its credential at `mcp/impersonator` beside the `provisioner`. On AWS the `warehouse` instance SHALL be configured with a `log_line_prefix` that includes `%u` and `%a`, so every log line carries the session user and the application name. Scaleway's Managed Databases accept no `log_line_prefix`, so on Scaleway the instance SHALL keep the engine's settings, and the person SHALL be named by `application_name` in `pg_stat_activity` and by the MCP audit.

#### Scenario: Log line for an impersonated query

- **WHEN** a person's query runs through the MCP on a Postgres warehouse deployed on AWS
- **THEN** the corresponding log line shows `impersonator` as the user and the person's warehouse name as the application name

#### Scenario: Impersonated query on Scaleway

- **WHEN** a person's query runs through the MCP on a Postgres warehouse deployed on Scaleway
- **THEN** `pg_stat_activity` shows `impersonator` as the user and the person's warehouse name as the application name, and the MCP audit records the query under the person

#### Scenario: Impersonator provisioned by the setup script

- **WHEN** a Postgres project with `mcp` is shipped
- **THEN** `impersonator` exists with `LOGIN NOINHERIT` and no other attribute, holds no membership, and its credential is in the secrets store at `mcp/impersonator`

### Requirement: Secrets and bucket prefixes are scoped per VM

Each VM SHALL run under its own cloud identity. Every secret SHALL have an environment-variable-style name whose prefix picks its scope: `MCP_*` the `mcp` scope, `DEPLOY_*` the `deploy` scope, `ADMIN_*` the `admin` scope, `SETUP_*` the `setup` scope and any other name the `platform` scope, stored in the cloud secrets store as `vecten/<project>/<env>/<scope>/<NAME>`; on Scaleway that is the secret `<NAME>` at the path `/vecten/<project>/<env>/<scope>`. The `setup` scope holds the database instances' admin credentials where the cloud does not manage them (Scaleway), and only the setup host reads it. The credentials this spec calls `mcp/provisioner` and `mcp/impersonator` are `MCP_PROVISIONER_PASSWORD` and `MCP_IMPERSONATOR_PASSWORD` in the `mcp` scope. The platform VM's identity SHALL read secrets in the `platform` scope only and write only the `platform/` bucket prefix; the MCP VM's identity SHALL read the `mcp` scope only, write only the `mcp/` prefix, and read the built-commit marker `platform/dbt/built_commit`. Under `minimal` the one VM SHALL hold both. No VM SHALL read the `deploy` or `admin` scope; the CI deploy identity reads and writes every scope. The setup host SHALL also create, write and read back exactly the generated credentials named in "Databases are private and set up from the VM". Bundles SHALL be written only by CI. A contract test SHALL assert the boundary on every provider.

#### Scenario: Secret name picks the scope

- **WHEN** `vecten secret set MCP_ALERT_TOKEN --env prod` and `vecten secret set HUBSPOT_API_KEY --env prod` run for project `acme`
- **THEN** the first is stored as `vecten/acme/prod/mcp/MCP_ALERT_TOKEN` and the second as `vecten/acme/prod/platform/HUBSPOT_API_KEY`

#### Scenario: No VM reads deploy credentials

- **WHEN** code on any VM asks the secrets store for `DEPLOY_SNOWFLAKE_PRIVATE_KEY` or an `ADMIN_*` password
- **THEN** the request is refused by the cloud

#### Scenario: DAG code reads the provisioner credential

- **WHEN** code on the platform VM asks the secrets store for `mcp/provisioner` or `mcp/impersonator`
- **THEN** the request is refused by the cloud

#### Scenario: Platform VM writes the MCP bundle

- **WHEN** code on the platform VM writes to the `mcp/` bucket prefix
- **THEN** the write is refused by the cloud

### Requirement: Scaleway scopes with a per-VM key

On Scaleway, which has no instance identity, each VM SHALL sign with an API key of its own IAM application, scoped to that VM's secret scopes and bucket prefixes in the environment's resources project and to nothing in its state project. `bootstrap` SHALL create the application, its policy and its key with an expiry, and store the key in the `deploy` scope. `deploy` SHALL hand the key to the VM through the server's instance metadata, set through the Instances API and never through Terraform, wait for the VM's watcher to acknowledge it, then delete it from the metadata, so neither Terraform state nor the user data Terraform sets ever holds it. The VM SHALL keep the key in a root-only file, and the runbook SHALL name this as the one place a long-lived key sits on a disk. A rerun of `bootstrap` within 30 days of a key's expiry SHALL rotate it, keeping the old key valid until the VM has acknowledged the new one.

#### Scenario: Scaleway platform VM

- **WHEN** code on a Scaleway platform VM uses the VM's key to read `mcp/provisioner`
- **THEN** the request is refused by Scaleway IAM

#### Scenario: Key handed over outside Terraform

- **WHEN** `deploy` hands a Scaleway VM its key
- **THEN** the key sits in that server's metadata only until the VM acknowledges it
- **AND** no Terraform state, plan or user data that Terraform sets holds it

#### Scenario: Key never acknowledged

- **WHEN** a Scaleway VM does not acknowledge its key within the window
- **THEN** `deploy` exits non-zero naming the host, and the key is deleted from the server's metadata all the same

### Requirement: Databases are private and set up from the VM

No managed Postgres instance SHALL be reachable from the internet, unless `database_public_cidrs` in tfvars opts in, and then only from the address ranges it lists. The databases, their owner roles, `loader`, `analyst`, `provisioner`, `impersonator`, the `REVOKE CONNECT` on every database and the provisioner's default privileges SHALL be created by an idempotent setup script that `ship` runs on the setup host with each instance's master credential, never by the Terraform module and never from a GitHub-hosted runner. The setup host SHALL be the MCP VM when `mcp` is a component, else the platform VM, and the one VM under `minimal`; it SHALL be the only VM that reads the instances' master credentials, so the VM running DAG code holds none when an MCP VM exists, and `ship` SHALL ship it before the other hosts. The setup host SHALL generate the database credentials the stack uses, `AIRFLOW_DB_PASSWORD`, `LOADER_PASSWORD`, `MCP_STATE_DB_PASSWORD`, `MCP_PROVISIONER_PASSWORD` and `MCP_IMPERSONATOR_PASSWORD`, each when its component and warehouse are present, store them in the secrets store and read them back on later ships, so a replaced instance gets the same credentials; its identity SHALL create, write and read exactly those names. On AWS the `log_line_prefix` SHALL be set by the module in the instance's parameter group, because RDS accepts server parameters only there; the Scaleway module sets none.

#### Scenario: Instance from the internet

- **WHEN** a connection is attempted to either instance from outside the environment's network while `database_public_cidrs` is empty
- **THEN** it is refused

#### Scenario: Setup is repeatable

- **WHEN** `ship` runs the setup script a second time
- **THEN** every role, database and privilege is unchanged and the script exits 0

#### Scenario: Master credential on the setup host only

- **WHEN** a project with both components is deployed under `standard`
- **THEN** the MCP VM's identity reads the instances' master secrets and writes the generated credentials
- **AND** the platform VM's identity is refused on the master secrets

### Requirement: Admin access goes through a tunnel or an opt-in endpoint

Admin access from a laptop SHALL go through a tunnel the cloud offers, SSM port forwarding on AWS. Scaleway offers no SSH and no run-command API to tunnel through, so admin access there SHALL go through the instances' public endpoint, SHALL verify the server against the instance's own certificate authority, and SHALL accept SCRAM authentication only. On either cloud a public endpoint with an address allow-list SHALL be an explicit opt-in in tfvars, `database_public_cidrs`, and the first deploy after the opt-in is cleared SHALL leave no public endpoint.

#### Scenario: Admin from the laptop on AWS

- **WHEN** `vecten user add-admin jane --env prod` runs on AWS
- **THEN** it reaches the `warehouse` instance through the cloud's tunnel and the master credential, and no public endpoint exists

#### Scenario: Admin on Scaleway without the opt-in

- **WHEN** `vecten user add-admin jane --env prod` runs on Scaleway while `database_public_cidrs` is empty
- **THEN** it exits non-zero naming `database_public_cidrs`, and creates nothing

#### Scenario: Admin on Scaleway with the opt-in

- **WHEN** `vecten user add-admin jane --env prod` runs on Scaleway after a deploy with `database_public_cidrs` listing the laptop's address
- **THEN** it connects to the `warehouse` instance's public endpoint with the server verified against the instance's certificate authority and SCRAM authentication, and the endpoint admits only the listed ranges

#### Scenario: Opt-in cleared

- **WHEN** `database_public_cidrs` is emptied and `deploy --env prod` runs
- **THEN** no instance of the environment has a public endpoint

### Requirement: Backups, deletion protection and a final snapshot

Every managed Postgres instance SHALL have automated backups kept seven days, with point-in-time recovery where the cloud offers it (AWS; Scaleway's Managed Databases keep daily backups instead), and `prod` instances SHALL have deletion protection where the cloud offers it. Scaleway's Managed Databases offer none, so there `deploy` SHALL refuse with exit 4, naming the instance and applying nothing, a cloud-root plan that deletes or replaces a database instance, and the final snapshot `destroy` takes is the databases' guard on destroy; the runbook SHALL describe replacing an instance by hand after a snapshot. `destroy` SHALL take a final snapshot of each instance and print its identifier before destroying, unless `--delete-data <env>` is given with the environment name. Where the cloud deletes snapshots on a date of its own (Scaleway keeps one at most 365 days and accepts no later date), `destroy` SHALL report that date and how to keep the data longer. Before any snapshot, `destroy` SHALL refuse with exit 4 while another run holds a root's Terraform state lock, naming who holds it, since when and the lock object. The runbook SHALL hold a restore section.

#### Scenario: Destroy keeps a snapshot

- **WHEN** `destroy --env prod --confirm prod` runs
- **THEN** a final snapshot of each instance exists and its identifier is printed before resources are destroyed

#### Scenario: Scaleway final snapshots expire

- **WHEN** `destroy --env prod --confirm prod` runs on Scaleway
- **THEN** the result names the date Scaleway deletes each final snapshot, and the message says to restore a snapshot and take a new one to keep the data past it

#### Scenario: Overlapping destroy

- **WHEN** `destroy --env prod --confirm prod` starts while an earlier destroy of `prod` still holds the state lock
- **THEN** it exits 4 having taken no snapshot and run no Terraform

#### Scenario: Explicit data deletion

- **WHEN** `destroy --env sandbox --confirm sandbox --delete-data sandbox` runs
- **THEN** no snapshot is taken

#### Scenario: Database replacement refused on Scaleway

- **WHEN** `deploy --env prod` runs on Scaleway and the plan replaces or deletes a database instance
- **THEN** the command exits 4 naming the instance, and nothing is applied

#### Scenario: Restore

- **WHEN** the restore section of the runbook is followed for `warehouse`
- **THEN** the instance is restored to the chosen point in time on AWS, or to the chosen snapshot on Scaleway, and the `state` instance is untouched

#### Scenario: Restore after a destroy

- **WHEN** the restore section is followed after `destroy` under `standard` with the `mcp` component
- **THEN** `warehouse` and `state` are both restored from the final snapshots that `destroy` took, and the MCP starts against them

#### Scenario: Restored RDS instance without a managed master secret

- **WHEN** Terraform is pointed at a restored AWS instance whose RDS-managed master secret is not turned on
- **THEN** Terraform refuses, naming the instance and the command that turns the secret on

### Requirement: Base images are published, the project layer is built on the VM

Vecten SHALL publish the Airflow and MCP base images to a public registry at each release, pinned in the versions table, each tag holding linux/amd64 and linux/arm64. `ship` SHALL build the thin project layer on the VM on top of the base image, installing the project's extensions and connector dependencies as `vecten.toml` declares them; a project carries no lockfile at v0.1. No per-project registry SHALL be required.

The MCP base image SHALL hold every third-party distribution at the version the repository's lockfile pins, the versions CI tests against, and its build SHALL fail rather than resolve anew when the lockfile does not match the workspace's package metadata. The Airflow base image SHALL resolve the platform packages against the distributions its pinned upstream image installs, since the lockfile holds no Airflow. Neither image's build SHALL leave behind an installer that the image would not hold without it; the MCP image SHALL keep pip, which the project layer installs with.

#### Scenario: Extension added

- **WHEN** a project adds an extension and ships
- **THEN** the VM builds a project layer holding it on top of the pinned base image and the profile starts with it

#### Scenario: Laptop on Apple Silicon

- **WHEN** a person runs `vecten up` from the published package on an arm64 Mac, with no checkout and no locally built base images
- **THEN** the project layer builds on the arm64 image of the pinned base image and the stack starts

#### Scenario: Published tag missing a platform

- **WHEN** a release's published `vecten-airflow` or `vecten-mcp` tag lacks linux/amd64 or linux/arm64
- **THEN** the release check exits non-zero, naming the tag and the platform it lacks, and the release does not proceed

#### Scenario: MCP image holds the locked versions

- **WHEN** the MCP base image built from a commit is inspected
- **THEN** every installed distribution other than `vecten-mcp`, `vecten-shared` and pip is at the version that commit's lockfile pins

#### Scenario: Stale lockfile

- **WHEN** the MCP base image is built from a checkout whose lockfile no longer matches a member's declared dependencies
- **THEN** the build fails, naming the lockfile, and no image is tagged

#### Scenario: No installer left behind

- **WHEN** either base image is inspected
- **THEN** the MCP image holds no uv, and the only uv in the Airflow image is the one its upstream image ships

### Requirement: Secrets never sit in Terraform state

The managed Postgres master password SHALL be managed by the cloud where offered (RDS on AWS). Otherwise (Scaleway) Terraform SHALL generate it in memory during the apply, from an ephemeral resource, and hand it to the instance and to its `SETUP_*` secret through write-only arguments, so neither the state nor a plan holds it; raising the instance's password version in tfvars SHALL set a new password on the instance and in its secret together. On Scaleway the secret SHALL be the truth: after every apply `deploy` SHALL set each instance's administrator to the password its `SETUP_*` secret holds, through the Managed Databases API, before it hands any VM its key. The provisioner and impersonator credentials SHALL be generated by the setup script on the VM, never by Terraform. The state bucket SHALL be private and versioned, and encrypted where the cloud offers it. The Snowflake Terraform credential, `DEPLOY_SNOWFLAKE_USER` and `DEPLOY_SNOWFLAKE_PRIVATE_KEY` in the `deploy` scope, SHALL live in the cloud secrets store and be read by the apply workflow under its deploy identity.

#### Scenario: State inspected

- **WHEN** the Terraform state is read
- **THEN** it holds no database password, provisioner or impersonator credential, and on Scaleway no VM key

#### Scenario: Scaleway apply stopped between the instance and its secret

- **WHEN** a Scaleway apply sets an instance's admin password and fails before writing its `SETUP_*` secret version, and the next `deploy --env prod` writes the secret version with a new password
- **THEN** after that deploy the instance and the secret hold the same password

### Requirement: Sizing tiers

A `size` of `small`, `medium` or `large`, derived from `users`, SHALL select, from a table in the package, the MCP VM class, the `warehouse` instance class and storage, the Snowflake warehouse size, and the per-tier connection budget on each cloud. The platform VM SHALL default to `small`, because it scales with concurrent syncs, not users, and SHALL be overridable in tfvars. The `state` instance SHALL be the smallest class that holds the state databases' connections, on every tier: `db.t4g.micro` on AWS and `DB-PLAY2-NANO` on Scaleway, whose smaller `DB-PLAY2-PICO` admits too few. Under `minimal` on a Postgres project with the platform, the `state` instance holds the warehouse and SHALL take the tier's warehouse storage and the tier's minimal class, which is the warehouse class on AWS and on Scaleway the warehouse class or, where that class is smaller than the state class, the state class, because the tier's connection budget is sized for that class and the state databases share it. Under `minimal` the one VM SHALL have at least 8 GB (`DEV1-L` on Scaleway), and a project under `minimal` SHALL cost no more than the same project under `standard`.

#### Scenario: Tier applied

- **WHEN** `deploy --env prod` runs with `size = "medium"` for a project with both components
- **THEN** the MCP VM and the `warehouse` instance are the medium entries of the package table for that cloud
- **AND** the platform VM is the small entry and the `state` instance the cloud's state class

#### Scenario: Connection budget

- **WHEN** the `large` tier is deployed with its documented user count
- **THEN** the pool caps sum to less than the instance's connection limit

### Requirement: Pools fit the connection budget, storage grows

Per-person connection pools and the Airflow pool SHALL be capped so their sum fits the tier's connection budget. Storage auto-grow SHALL be enabled on AWS; Scaleway SHALL get a fixed volume per tier. Changing `size` and running `deploy` SHALL resize in place.

#### Scenario: Resize

- **WHEN** `size` changes from `small` to `medium` and `deploy --env prod` runs
- **THEN** the plan shows the VM and instance class changes and no resource is destroyed

### Requirement: Per-cloud services

Each module SHALL use these services: AWS: EC2 with Elastic IP, RDS, Secrets Manager, S3, Route 53. Scaleway: Instance with flexible IP, Managed Database for PostgreSQL, Secret Manager, Object Storage, Domains and DNS.

#### Scenario: AWS resources

- **WHEN** the AWS module is applied
- **THEN** each VM is an EC2 instance with an Elastic IP, Postgres is RDS, secrets are in Secrets Manager, the bucket is S3 and the records are in Route 53

#### Scenario: Scaleway resources

- **WHEN** the Scaleway module is applied
- **THEN** each VM is an Instance with a flexible IP, Postgres is Managed Database for PostgreSQL, secrets are in Secret Manager, the bucket is Object Storage and the records are in Domains and DNS

### Requirement: Identical module outputs

Every cloud module SHALL expose the same set of Terraform outputs, and the provider SHALL build connection strings and locate each VM, the bucket and the secrets store from those outputs alone.

#### Scenario: Output parity

- **WHEN** the two modules' outputs are compared
- **THEN** the output names and types are identical

#### Scenario: Connection string from outputs

- **WHEN** the provider is asked for the `warehouse` connection of a deployed environment
- **THEN** it is built from module outputs without querying the cloud API

### Requirement: Snowflake module

When the warehouse is Snowflake, `deploy` SHALL also apply a Snowflake module that creates a database, the schemas named by the convention, an `analyst` role, a `loader` role used by dlt and dbt, a `provisioner` role when `mcp` is a component, and a warehouse sized by the tier with auto-suspend, in the user's existing Snowflake account. The role names, the loader user and the database name are account-wide, so one Snowflake account SHALL serve one environment, and the runbook SHALL say so. `destroy` SHALL keep the Snowflake database unless `--delete-data <env>` is given.

#### Scenario: Snowflake project deploy

- **WHEN** `deploy --env prod` runs for a Snowflake project with both components
- **THEN** the cloud module and the Snowflake module are both applied
- **AND** the database holds `staging`, `snapshots`, `core` and `marts` schemas, the `analyst`, `loader` and `provisioner` roles and a warehouse

#### Scenario: Postgres project deploy

- **WHEN** `deploy --env prod` runs for a Postgres project
- **THEN** no Snowflake module is applied

#### Scenario: One environment per Snowflake account

- **WHEN** the runbook of a Snowflake project is read
- **THEN** it states that one Snowflake account serves one environment and names the account-wide roles, user and database

#### Scenario: Destroy keeps the Snowflake database

- **WHEN** `destroy --env prod --confirm prod` runs for a Snowflake project
- **THEN** the Snowflake module is not destroyed and the result says it kept the database

### Requirement: Provisioner and admin roles

When `mcp` is a component, the setup script `ship` runs SHALL create the `provisioner` role with the scope the identity contract states and store its credential in the secrets store at `mcp/provisioner`. `admin_<firstname>` warehouse accounts SHALL be created only by `vecten user add-admin <firstname>` over the master credential, through the admin access path "Admin access goes through a tunnel or an opt-in endpoint" states, never by the `mcp` component.

#### Scenario: Provisioner after ship

- **WHEN** `ship` completes for a project with `mcp` in `components`
- **THEN** the warehouse holds a `provisioner` role with no grant beyond the identity contract's scope
- **AND** its credential is readable only from `mcp/provisioner` by the MCP VM's identity

#### Scenario: Admin account

- **WHEN** `vecten user add-admin jane` runs with the master credential available
- **THEN** the warehouse holds `admin_jane` with full administrative rights: `rds_superuser` on AWS, and on Scaleway, whose Managed Databases grant no superuser, membership in the roles that own and load every warehouse object
- **AND** the `mcp` component has no record of it

#### Scenario: Platform-only project

- **WHEN** `ship` runs for a project without `mcp` in `components`
- **THEN** no `provisioner` role is created

### Requirement: MCP inputs stored only in the secrets store

For a project with `mcp` in `components`, the deploy SHALL store the provisioner credential at `mcp/provisioner` in the environment's secrets store, and nowhere else. It MUST NOT appear in Terraform state outputs, container images or logs.

#### Scenario: Secrets placed

- **WHEN** the deploy completes
- **THEN** `mcp/provisioner` exists in the secrets store
- **AND** its value appears in no Terraform output, built image or deploy log

### Requirement: One warehouse per deployment

A deployment SHALL serve exactly one warehouse, Postgres or Snowflake, chosen at `init`. A second warehouse MUST be a second deployment.

#### Scenario: Second warehouse configuration

- **WHEN** `vecten.toml` or the tfvars name two warehouse configurations
- **THEN** the deploy rejects the input and states that a second warehouse is a second deployment

### Requirement: Encryption and transport defaults

Every cloud module SHALL enable encryption at rest on every managed Postgres instance, and every hostname SHALL be served over TLS. Container images MUST contain no secrets.

#### Scenario: Image inspection

- **WHEN** the built images are inspected
- **THEN** they hold no credential, key or token

#### Scenario: Plain HTTP

- **WHEN** a client requests `http://mcp.<domain>/`
- **THEN** it is redirected to HTTPS and nothing but the certificate challenge is served over plain HTTP

### Requirement: Per-environment configuration and state

Each environment SHALL have its own `deploy/envs/<env>.tfvars`, carrying domain and DNS zone, region, GitHub repository, components and profile, and the Snowflake account for Snowflake projects, and its own Terraform state in a per-environment bucket in the chosen cloud's object storage. On Scaleway that bucket SHALL sit in a state project of its own, apart from the environment's resources project, so that no VM key, which reaches the resources project only, can reach the state; `bootstrap` SHALL claim each project by a marker in its description and refuse a project of the expected name that carries another. `prod` is filled from the deploy answers by `init` or `init --deploy`; other environments copy it.

#### Scenario: Prod from the deploy answers

- **WHEN** the deploy layer has been rendered by `init` or `init --deploy`
- **THEN** `deploy/envs/prod.tfvars` is complete and `deploy --env prod` needs no edit to it

### Requirement: State bucket is created by bootstrap

`bootstrap` SHALL create the environment's state bucket before the first `terraform init`, because a bucket the module creates cannot hold the module's own state, and `destroy` SHALL leave it in place.

#### Scenario: Two environments

- **WHEN** `staging` and `prod` are both deployed
- **THEN** each has its own tfvars file and its own state in the cloud's object storage
- **AND** destroying one leaves the other untouched

#### Scenario: First deploy

- **WHEN** `deploy --env prod` runs for the first time
- **THEN** the state bucket exists before `terraform init` runs
- **AND** the artifacts and logs bucket is created by the apply

#### Scenario: Destroy keeps state

- **WHEN** `destroy --env prod` completes
- **THEN** the state bucket still exists and holds the final state

### Requirement: CI deploys over OIDC where the cloud offers it, applying only on dispatch

The rendered GitHub Actions workflows SHALL authenticate to the chosen cloud through OIDC with no long-lived keys where the cloud offers OIDC federation (AWS). Scaleway IAM offers none, so there `bootstrap` SHALL store scoped API keys as GitHub secrets: the deploy key as secrets of the GitHub environment, which GitHub hands only to jobs that declare it, and the plan key as repository secrets. Neither key SHALL hold IAM rights, both SHALL expire, a rerun of `bootstrap` SHALL rotate them, and a workflow SHALL hand a key only to the step that runs `vecten`. Pull requests and pushes to main SHALL run the plan only, under a read-only plan identity that reads no secret and writes nothing but the Terraform state lock, and SHALL plan the cloud root only; a Snowflake root is planned when the apply workflow is dispatched. The Scaleway plan identity reads nothing outside the state, so pull-request plans there SHALL run without a refresh. The apply workflow, which applies and then ships each component, SHALL run only when dispatched, under the deploy identity, which trusts only jobs that declare the environment. Only the chosen cloud's workflows SHALL be rendered. Apply, deploy and ship runs SHALL share one concurrency group per environment, so two runs never apply or ship to the same environment at once.

#### Scenario: Pull request

- **WHEN** a pull request changes `deploy/`
- **THEN** the plan workflow runs under the plan identity and posts the plan of the cloud root, and nothing is applied

#### Scenario: Push to main

- **WHEN** a change is pushed or merged to main
- **THEN** the plan workflow runs and nothing is applied

#### Scenario: Dispatched deploy

- **WHEN** `vecten deploy --env prod` dispatches the apply workflow
- **THEN** the plan is applied and every component in `components` is shipped

#### Scenario: Two dispatches for one environment

- **WHEN** `vecten deploy --env prod` is dispatched while an earlier prod apply is still running
- **THEN** the second run waits for the first to finish and never runs Terraform at the same time

#### Scenario: Credentials in the repository

- **WHEN** the rendered workflows and secrets of an AWS project are inspected
- **THEN** no cloud access key or service account key is stored; trust is OIDC only

#### Scenario: Credentials in the repository on Scaleway

- **WHEN** the rendered workflows and secrets of a Scaleway project are inspected
- **THEN** the only cloud keys are the deploy key in the GitHub environment's secrets and the plan key in the repository's secrets, both expiring and neither holding IAM rights
- **AND** the plan job declares no environment, and each key reaches only the step that runs `vecten`

### Requirement: deploy refuses a commit that is not on main

`deploy --env <env>` run from a laptop SHALL refuse when the checked-out commit is not on the remote default branch, naming the branch, so production never runs code review did not see. Terraform backend locking SHALL be verified on each cloud before its release test.

#### Scenario: Deploy from a feature branch

- **WHEN** `deploy --env prod` runs on a commit not on `origin/main`
- **THEN** it exits non-zero and names the branch

### Requirement: Content ships on merge without Terraform

A rendered ship workflow SHALL run on every push to main once the environment is deployed. It SHALL run `vecten ship --env <env>`, which builds one content bundle, the DAGs, the dbt project, `osi/` and `evals/` at that commit, ships it to every component host through the cloud's `ship` mechanism, and runs no Terraform and creates nothing billable. The bundle SHALL record its commit, and every host SHALL keep the previous bundle to roll back to when the health poll fails.

#### Scenario: Merged metric fix reaches both hosts

- **WHEN** a pull request editing `osi/metrics.json` and a dbt mart merges to main
- **THEN** the ship workflow places the bundle at that commit on the platform host and the MCP host without anyone dispatching `deploy`

#### Scenario: Ship before deploy

- **WHEN** a push to main happens before `deploy --env prod` has ever run
- **THEN** the ship workflow exits without shipping and says the environment is not deployed

#### Scenario: Health poll fails after ship

- **WHEN** a shipped bundle leaves a component's URL unanswered or not healthy (Airflow: HTTP 200 with a healthy metadatabase; the MCP: HTTP 200 from `/health`)
- **THEN** the host restores the previous bundle and the workflow fails naming the component

#### Scenario: Agent fails after the swap

- **WHEN** the VM agent fails after it has pointed `current` at the new bundle
- **THEN** the agent itself restores the previous bundle and NGINX, and the workflow fails naming the step and the commit it rolled back to

#### Scenario: A container fails at startup

- **WHEN** the MCP container exits at startup and restarts in a loop, so `compose up` fails
- **THEN** `vecten ship` and the workflow print, after the failure, the last log lines of each failing container with its service name, every secret value the agent injected masked and the rest redacted, without anyone opening a session on the VM

#### Scenario: Compose fails before a container runs

- **WHEN** `docker compose up` fails on a host before any container is failing, as when an image cannot be pulled or built
- **THEN** `vecten ship` and the workflow print, after the failure, the last lines of compose's own output, masked and redacted the same way

### Requirement: The dbt DAG records the commit it built

The rendered dbt DAG SHALL run from the shipped bundle and, after a green build, SHALL write the bundle's commit as the environment's built commit to a location the MCP host may read, so the `mcp` component can tell which model matches the warehouse. On both clouds that location SHALL be `platform/dbt/built_commit` in the environment's bucket.

#### Scenario: Built commit advances

- **WHEN** the dbt DAG completes a green build from the bundle at commit `a3f9c2e`
- **THEN** the built commit for the environment reads `a3f9c2e`

#### Scenario: Red build

- **WHEN** the dbt DAG fails
- **THEN** the built commit is unchanged

### Requirement: Deploy facts are committed, run records are not

The CLI SHALL record non-secret deploy facts, whether an environment is bootstrapped, its last deployed commit and whether the first user was added, under `[environments.<env>]` in `vecten.toml`, so a second clone of the project sees the same `next`. Run records that only the laptop needs stay under `.vecten/`.

#### Scenario: Second clone

- **WHEN** a colleague clones a project whose `prod` is deployed and has users
- **THEN** `next` does not offer `bootstrap`, the deploy ask or the first-user ask again

### Requirement: Host identities read no Parameter Store values

On AWS the permissions boundary every host role carries SHALL allow the actions the Systems Manager agent needs to receive and run commands, and SHALL NOT allow reading Parameter Store values, so code holding a host's identity cannot read parameters that belong to other workloads in the account.

#### Scenario: Unrelated parameter

- **WHEN** code on a VM asks Parameter Store for a parameter its environment did not create
- **THEN** the request is refused by the boundary

#### Scenario: Ship still reaches the VM

- **WHEN** CI ships a bundle to an AWS VM over Run Command
- **THEN** the VM's agent receives and runs the command

### Requirement: The MCP server's Postgres connections require TLS

Outside `up`, the MCP server SHALL require TLS on its connection to the state database and on its connections to a Postgres warehouse, and SHALL refuse to connect when the server offers no TLS.

#### Scenario: State database without TLS

- **WHEN** the deployed MCP server connects to a state database that offers no TLS
- **THEN** the connection is refused rather than made in plain text

#### Scenario: Local stack

- **WHEN** `up` starts the MCP server against the local Postgres container
- **THEN** the server connects without requiring TLS
