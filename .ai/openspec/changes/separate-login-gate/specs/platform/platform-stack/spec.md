## ADDED Requirements

### Requirement: Independent service gate

A project with a bound login route SHALL run the service-host gate in a separate container using the MCP image. The container SHALL have a dedicated database login and a dedicated network, receive no warehouse or provider secrets, and run on a bridge guarded against metadata access on a VM. Session verification SHALL continue while the MCP process restarts.

#### Scenario: State access is limited to the gate

- **WHEN** the standalone gate connects to the state database
- **THEN** it can read principals, memberships and app admission, spend gate codes and write its own session and logout state. PostgreSQL refuses writes to principals, memberships and the issuer's browser and panel sessions.

#### Scenario: The MCP process stops

- **WHEN** a service receives a valid gate session while the MCP process is down
- **THEN** the gate checks the current state and forwards the same authorized viewer identity

#### Scenario: A new browser signs in

- **WHEN** the browser starts a service login
- **THEN** the gate hands it to the issuer and redeems the resulting one-time code at the service hostname

## MODIFIED Requirements

### Requirement: Compose services are grouped into profiles

The stack SHALL define `platform` (Airflow API server, scheduler, DAG processor, triggerer and log cleaner), `mcp` (MCP server and, for a bound login route, login gate) and `local` (Postgres), with NGINX in every profile. The other services SHALL be bound extensions in their host component's profile. Local up SHALL start the configured components and local profile. A VM SHALL run its assigned components and never the local profile.

#### Scenario: Both components on a laptop

- **WHEN** `up` completes in a project with both components and no bound service
- **THEN** the `platform`, `mcp` and `local` profiles are running: the five platform services, `mcp-server`, `postgres` and `nginx`

#### Scenario: MCP-only on a laptop

- **WHEN** `up` completes in a project whose `components` is `mcp` only
- **THEN** `mcp-server`, `postgres` and `nginx` are running and no Airflow service exists

#### Scenario: Standard profile on VMs

- **WHEN** a project with both components under the `standard` profile is deployed
- **THEN** the platform VM runs the `platform` profile and `nginx`, the MCP VM runs the `mcp` profile and `nginx`, and neither runs `postgres`

#### Scenario: Bound service on the platform host

- **WHEN** a project under `standard` binds one service with `host = "platform"` and is deployed
- **THEN** the service runs on the platform VM and not on the MCP VM

### Requirement: NGINX routes by hostname

NGINX SHALL route the Airflow hostname to Airflow and the MCP hostname to the MCP server for its MCP endpoint, OAuth, gate authorization and admin panel. On the MCP hostname, the service gate's start, callback, logout and verify paths SHALL go to the separate gate. A bound service hostname SHALL serve only its bound routes, gate paths and health path. Unknown hosts and paths SHALL be refused. Both built-in names SHALL be issued under every profile and local up SHALL print the started routes.

#### Scenario: Airflow host

- **WHEN** a client requests `https://airflow.<domain>/`
- **THEN** the Airflow UI is served

#### Scenario: MCP host

- **WHEN** a client requests `https://mcp.<domain>/`
- **THEN** the MCP server answers, with the MCP endpoint, login and consent pages and the panel under it

#### Scenario: Service host

- **WHEN** a client requests `https://portal.<domain>/` and `portal` is bound to a service with a route at `/`
- **THEN** the service answers, subject to the route's access

#### Scenario: Other host or path

- **WHEN** a client requests an unknown hostname or a path no component or bound route serves
- **THEN** the request is refused with an HTTP error, not proxied

#### Scenario: Minimal profile routing

- **WHEN** both components and one service run on one VM
- **THEN** one `nginx` routes `airflow.<domain>`, `mcp.<domain>` and the service's hostname to their services on that host
