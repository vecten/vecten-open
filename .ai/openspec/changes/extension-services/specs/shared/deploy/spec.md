## ADDED Requirements

### Requirement: Service hostnames reach DNS through deploy and certificates through ship

`vecten deploy`, including `--plan-only`, SHALL pass the cloud root the variable `service_hostnames`, a map from each hostname label bound in `vecten.toml` to the `host` component of its service, and SHALL pass nothing when no hostname is bound. The scaffolded thin root SHALL declare the variable with an empty default and pass it to the module. When a hostname is bound and the root does not declare the variable, `deploy` SHALL refuse before Terraform runs, naming `vecten update`. Each cloud module SHALL validate the map (label shape, reserved labels, `@` refused where the domain equals the DNS zone, components among `components`), SHALL create one A record per label, `<label>.<domain>` or the domain itself for `@`, pointing at the host that serves the component, beside the built-in records, and SHALL list the names each host serves in the output field `hosts[<host>].service_hostnames`, identically on both clouds. `ship` SHALL append a host's `service_hostnames` to `host.json`'s `hostnames`, so the VM agent issues and the renewal timer renews their certificates as it does for the built-in names. A bound hostname absent from the outputs SHALL NOT be passed to the agent or rendered in NGINX; `ship` SHALL report its routes as waiting for `vecten deploy --env <env>`, and `next` SHALL offer that command.

#### Scenario: Portal hostname on AWS

- **WHEN** a project binds `hostname = "portal"` for a service on `platform` and `vecten deploy --env prod` runs on AWS under `standard`
- **THEN** Route 53 holds `portal.<domain>` pointing at the platform VM's Elastic IP, and the outputs list it under the platform host's `service_hostnames`

#### Scenario: Same hostname on Scaleway under minimal

- **WHEN** the same project deploys on Scaleway with `profile = "minimal"`
- **THEN** Domains and DNS holds `portal.<domain>` pointing at the one VM's flexible IP

#### Scenario: Certificate on the next ship

- **WHEN** `ship --env prod` runs after that deploy
- **THEN** the VM holds a Let's Encrypt certificate for `portal.<domain>` and serves the portal's routes over HTTPS

#### Scenario: Route added before its deploy

- **WHEN** a merge binds a new hostname `bot` and the ship workflow runs before `vecten deploy --env prod`
- **THEN** the ship succeeds, the service runs, its routes are not served, the ship's output reports them as waiting for `vecten deploy --env prod`, and `next` offers that command

#### Scenario: Project without services

- **WHEN** a project binds no hostname and `vecten deploy --env prod` runs
- **THEN** the plan holds no change caused by this requirement, and `host.json` lists only the built-in hostnames

### Requirement: The VM agent isolates services and hands them their secrets

`host.json` SHALL carry `service_secrets`, the secret names that the bound services on the host declare within the scopes the host may read, and `service_network`, the bridge name of the services' network, or empty when the host runs no service. When `service_network` is set, the agent SHALL, on every ship, at boot before Docker starts, and before Compose starts, ensure `DOCKER-USER` rules that drop traffic from that bridge to the cloud's instance metadata address (169.254.169.254 on AWS, 169.254.42.42 on Scaleway), and SHALL fail the step, naming it, when it cannot. The agent SHALL read each name in `service_secrets` from its scope, hand the values to Compose's environment only, and mask them in every line `ship` prints. A release whose `host.json` lacks either key SHALL be treated as having no service. The agent SHALL stay Python 3.9 standard library only, and its Terraform copy byte-identical.

#### Scenario: Ship with a bot on AWS

- **WHEN** a bundle binding `acme-bot`'s service, whose manifest declares `ACME_BOT_TOKEN`, is shipped to an AWS platform VM
- **THEN** the drop rule for the services' bridge exists before the service starts, and the service's environment holds `ACME_BOT_TOKEN` read from the `platform` scope

#### Scenario: Reboot

- **WHEN** the VM reboots
- **THEN** the rule is in place before Docker restarts any service, and the agent's boot step ensures it again before it starts the stack

#### Scenario: Rule cannot be installed

- **WHEN** the agent's iptables command fails on a host with a bound service
- **THEN** the ship fails naming the isolation step, and the previous release is restored

#### Scenario: Rollback to a bundle without services

- **WHEN** the agent restores a release built before this change
- **THEN** it starts that release without error and installs no rule

### Requirement: Ship reports services without rolling back for them

After the component polls, `ship` SHALL poll `https://<hostname>/_vecten/health` for each served service hostname that has a health path, and SHALL report each bound service as `healthy`, `unhealthy`, `waiting for deploy`, `not routed` (no hostname bound) or `served` (its hostname is served and it declares a health command, which only Compose runs), in its output and with `--json`. A service's state SHALL NOT fail the ship or restore the previous bundle. `ship` SHALL also report named volumes of services no longer bound on the host.

#### Scenario: Unhealthy portal

- **WHEN** a shipped bundle's portal answers 500 on its health path while Airflow and the MCP pass their polls
- **THEN** `ship` exits 0, the bundle stays, and the output reports the portal as `unhealthy`

## MODIFIED Requirements

### Requirement: Components and profile

An environment SHALL carry the components named by `components` in `vecten.toml`: `platform`, `mcp` or both. `profile` in `vecten.toml` SHALL be `standard`, the default, or `minimal`. Under `standard` each component runs on its own VM and the state databases on their own instance; under `minimal` one VM and one instance carry everything. The hostnames `airflow.<domain>` and `mcp.<domain>` SHALL be issued under every profile, and each service hostname `vecten.toml` binds SHALL resolve to the VM that runs the service's `host` component.

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

#### Scenario: Service hostname under standard

- **WHEN** `deploy --env prod` runs for a project with both components that binds `portal` to a service on `mcp`
- **THEN** `portal.<domain>` resolves to the MCP VM

### Requirement: Environment shape

Every environment SHALL have: the Postgres instances the profile requires; one VM per host with its own cloud identity; a secrets store and the identities GitHub Actions deploys and plans with, both created by `bootstrap`, which are an OIDC trust on AWS and, on Scaleway, whose IAM offers no OIDC federation, scoped API keys stored as GitHub secrets; an object storage bucket with a prefix per component; DNS records for both built-in hostnames and for each bound service hostname, with TLS on each VM for the names it serves; and one network with inbound 80 and 443 only.

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

#### Scenario: Service hostname certificate

- **WHEN** a bound service hostname's record exists and its host has been shipped
- **THEN** that host holds a Let's Encrypt certificate for the service hostname, and no other host does
