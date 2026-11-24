## MODIFIED Requirements

### Requirement: Secrets and bucket prefixes are scoped per VM

Each VM SHALL have a runtime identity restricted to its enabled components. Secret names SHALL select the scope by prefix (`MCP_*`, `DEPLOY_*`, `ADMIN_*`, `SETUP_*`, else platform). Their location SHALL remain `vecten/<project>/<env>/<scope>/<NAME>`, or on Scaleway the secret `<NAME>` at path `/vecten/<project>/<env>/<scope>`. Runtime SHALL read only its component scopes, including both enabled scopes under minimal. MCP provisioner and impersonator password names SHALL remain `MCP_PROVISIONER_PASSWORD` and `MCP_IMPERSONATOR_PASSWORD`. Runtime SHALL have no access to database master values, setup, admin, deploy or state, and no ability to obtain or assume an identity granting such access. Runtime secret writes SHALL be confined to the MCP signing key and impersonator rotation. The setup agent alone among VM callers SHALL use separate setup authority to read master secrets and initialize the exact credentials required by the layout.

Hosts SHALL retain component-prefix object reads and writes, artifacts bucket listing, MCP reads of the three platform dbt build records, and host-agent reads of their own bundles. Only CI among deployed workloads and workflows SHALL write bundles. The CI deploy identity SHALL retain its environment's secret scope access. Contract tests SHALL cover each provider.

#### Scenario: Secret name picks the scope

- **WHEN** the operator sets MCP_ALERT_TOKEN and HUBSPOT_API_KEY for acme in prod
- **THEN** the first secret is in the mcp scope and the second is in platform

#### Scenario: No VM reads deploy credentials

- **WHEN** a runtime or setup identity requests DEPLOY_SNOWFLAKE_PRIVATE_KEY or an ADMIN_* password
- **THEN** the cloud refuses the read

#### Scenario: DAG code reads the provisioner credential

- **WHEN** code on a dedicated platform VM requests the MCP provisioner or impersonator secret
- **THEN** the cloud refuses the read

#### Scenario: Platform VM writes the MCP bundle

- **WHEN** a dedicated platform VM writes to the mcp/ prefix or any VM writes to bundles/
- **THEN** the cloud refuses the write

#### Scenario: Components share a VM

- **WHEN** an Airflow task or MCP process on a minimal VM uses its actual cloud credential to read a managed Postgres master or overwrite a setup-only generated password
- **THEN** the provider refuses access even though setup succeeded on the same VM

### Requirement: Scaleway scopes with a per-VM key

Bootstrap SHALL create an expiring key per runtime application and a separate expiring setup application key in the resources project. Each key SHALL be stored separately in the deploy scope and have no state project access. Deploy SHALL deliver the runtime key per host and the setup key only to the setup host, in separate temporary Instances API user-data entries outside Terraform. The agent SHALL keep each key in a separate root-only file. Only the runtime key SHALL enter Compose. The runbook SHALL identify both persistent key locations. Bootstrap within 30 days of expiry SHALL rotate each independently and retire its predecessor only after verified receipt on the intended VM. Deploy SHALL clear both entries after delivery or failure. A subsequent apply SHALL clean up after interruption before refresh, and any refresh with remaining entries SHALL be refused.

#### Scenario: Scaleway platform VM

- **WHEN** a dedicated platform runtime key requests an MCP provisioner value
- **THEN** Scaleway IAM refuses the read

#### Scenario: Key handed over outside Terraform

- **WHEN** deploy delivers both keys to the setup host
- **THEN** each receipt identifies the server, key and purpose and deploy verifies possession
- **AND** neither Terraform state, plan nor Terraform-managed user data contains a key

#### Scenario: Key never acknowledged

- **WHEN** either required receipt is missing, stale or from another server
- **THEN** deploy fails naming the host and purpose and removes both entries
- **AND** bootstrap retains the preceding valid key for that purpose

### Requirement: Databases are private and set up from the VM

Managed Postgres SHALL be private unless database_public_cidrs explicitly allows the listed addresses. Ship SHALL run idempotent setup SQL from the private network on the MCP VM when present, otherwise the platform VM, or the sole minimal VM. It SHALL ship this host first. The setup agent SHALL use each instance master for its short-lived SQL invocation and create the databases, owner roles, loader, analyst, provisioner and impersonator as applicable, the revocation of CONNECT from PUBLIC on each database and the provisioner's default privileges defined in the [setup SQL](../../../../../../../packages/vecten-shared/src/vecten_shared/sql/warehouse_setup.sql). On AWS Terraform SHALL set log_line_prefix in the RDS parameter group. Scaleway SHALL keep its instance defaults.

Setup SHALL initialize and reread only the generated database passwords the layout needs (AIRFLOW_DB_PASSWORD, LOADER_PASSWORD, MCP_STATE_DB_PASSWORD, MCP_PROVISIONER_PASSWORD and MCP_IMPERSONATOR_PASSWORD), and initialize the five platform Airflow application secrets when platform is enabled. It SHALL preserve existing values on retry, restore and replacement. Master values SHALL be confined to the agent and the one setup invocation, and setup cloud credentials SHALL stay out of all containers, bundles, logs and Terraform state.

#### Scenario: Instance from the internet

- **WHEN** database_public_cidrs is empty
- **THEN** a connection from outside the private network is refused

#### Scenario: Setup is repeatable

- **WHEN** ship runs setup a second time
- **THEN** it succeeds with the same roles, privileges and existing application credentials

#### Scenario: Master credential on the setup host only

- **WHEN** both components run under standard
- **THEN** the setup agent on the MCP VM can read the managed masters
- **AND** the actual runtime credentials on both VMs are refused by the cloud

## ADDED Requirements

### Requirement: AWS containers use renewable runtime sessions

The EC2 profile SHALL be available only to host processes. All container networks SHALL be isolated from metadata before workloads start, including on reboot. Airflow and MCP SHALL receive renewable temporary credentials restricted to the assigned runtime role. Failure of runtime credential delivery SHALL never permit a fallback to setup authority.

#### Scenario: Session expires

- **WHEN** a long-running Airflow or MCP client crosses its first session expiry
- **THEN** normal reads succeed with a refreshed runtime session
- **AND** after renewal stops and the last session expires, cloud operations fail closed

### Requirement: Existing environments can cut over without replacing databases

An upgrade SHALL establish the new identities and compatible host launch path before retiring legacy access, preserve existing application credentials and require a verified cutover on every active host. After cutover, ship, boot, restore and rollback SHALL enforce container isolation. Incompatible releases SHALL be refused before launch.

#### Scenario: Interrupted rollout

- **WHEN** migration stops after policy reduction or a subsequent ship fails its health poll
- **THEN** an operator can retry setup or restore a compatible release without regranting a legacy identity master access
- **AND** a host with missing prerequisites reports the recovery step without starting insecure workloads
