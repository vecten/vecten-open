# platform/platform-stack Specification

## Purpose
The Docker Compose stack that runs both components on a laptop and on cloud VMs: its services grouped into profiles, the NGINX front door per hostname, how secrets reach it, what differs between the laptop and a VM, and the machines it is sized for.

## Requirements

### Requirement: Compose services are grouped into profiles

The stack SHALL define the profiles `platform` (`airflow-api-server`, `airflow-scheduler`, `airflow-dag-processor`, `airflow-triggerer`, `log-cleaner`), `mcp` (`mcp-server`) and `local` (`postgres`), with `nginx` in every profile, and no other services. `up` SHALL start the profiles that `components` names plus `local`. A VM SHALL run the profile of the component shipped to it and never the `local` profile.

#### Scenario: Both components on a laptop

- **WHEN** `up` completes in a project with both components
- **THEN** the `platform`, `mcp` and `local` profiles are running: the five platform services, `mcp-server`, `postgres` and `nginx`

#### Scenario: MCP-only on a laptop

- **WHEN** `up` completes in a project whose `components` is `mcp` only
- **THEN** `mcp-server`, `postgres` and `nginx` are running and no Airflow service exists

#### Scenario: Standard profile on VMs

- **WHEN** a project with both components under the `standard` profile is deployed
- **THEN** the platform VM runs the `platform` profile and `nginx`, the MCP VM runs the `mcp` profile and `nginx`, and neither runs `postgres`

### Requirement: State lives on the managed Postgres

On a VM, Airflow metadata SHALL live in the `airflow` database and MCP state in the `mcp_state` database of the environment's managed Postgres, so a VM holds no state and can be rebuilt. On the laptop the `local` profile's `postgres` SHALL hold Airflow metadata, MCP state and, for Postgres projects, the warehouse.

#### Scenario: VM rebuilt

- **WHEN** a platform VM is destroyed and recreated by `deploy`
- **THEN** Airflow's DAG history, connections and variables are intact after the stack starts

#### Scenario: Laptop state

- **WHEN** a Postgres project runs on the laptop
- **THEN** connectors and dbt use the `local` profile's `postgres` as the warehouse
- **AND** Airflow and the MCP server use databases on the same container

#### Scenario: Laptop Postgres matches the managed one for identity

- **WHEN** the `local` profile's `postgres` starts for a project with `mcp`
- **THEN** it runs the same major version as the managed Postgres, is started with a `log_line_prefix` including `%u` and `%a`, and has the `provisioner`, `impersonator` and `analyst` roles created by its init script

### Requirement: Airflow version and executor

The stack SHALL run Airflow 3.x with the LocalExecutor. The Airflow version SHALL be pinned in the scaffold and change only through a scaffold release applied with `update`.

#### Scenario: Pinned version

- **WHEN** two projects are rendered from the same scaffold version
- **THEN** both run the same Airflow version

### Requirement: NGINX routes by hostname

NGINX SHALL route `airflow.<domain>` to the Airflow UI and `mcp.<domain>` to the MCP server, which serves the MCP endpoint, the OAuth pages and the admin panel, and SHALL refuse any other host or path. Both hostnames SHALL be issued under every profile. On the laptop `up` SHALL print one URL per started component, distinguished by port or by local hostnames.

#### Scenario: Airflow host

- **WHEN** a client requests `https://airflow.<domain>/`
- **THEN** the Airflow UI is served

#### Scenario: MCP host

- **WHEN** a client requests `https://mcp.<domain>/`
- **THEN** the MCP server answers, with the MCP endpoint, login and consent pages and the panel under it

#### Scenario: Other host or path

- **WHEN** a client requests an unknown hostname or a path neither component serves
- **THEN** the request is refused with an HTTP error, not proxied

#### Scenario: Minimal profile routing

- **WHEN** both components run on one VM
- **THEN** one `nginx` routes `airflow.<domain>` and `mcp.<domain>` to the two profiles on that host

### Requirement: TLS on cloud and locally

On a cloud deployment NGINX SHALL serve HTTPS with a Let's Encrypt certificate for each hostname it serves, SHALL answer the HTTP-01 challenge on port 80, and SHALL redirect every other plain HTTP request to HTTPS. On the laptop it SHALL serve HTTPS with a self-signed certificate.

#### Scenario: Deployed environment

- **WHEN** `https://airflow.<domain>/` or `https://mcp.<domain>/` is opened
- **THEN** the connection is HTTPS with a certificate issued by Let's Encrypt for that hostname

#### Scenario: Plain HTTP

- **WHEN** a client requests `http://airflow.<domain>/`
- **THEN** it is redirected to the HTTPS URL and nothing is served over plain HTTP

#### Scenario: Local stack

- **WHEN** a URL printed by `up` is opened
- **THEN** the connection is HTTPS with a self-signed certificate

### Requirement: Airflow's own login guards the Airflow host

NGINX SHALL pass every path on the Airflow host to Airflow without HTTP authentication of its own, and Airflow's auth manager SHALL require its login for the UI and the API; its admin password SHALL come from the secrets provider, not from a file in the project. Airflow's health path `/api/v2/monitor/health` answers without a login, so the ship health poll reads Airflow's own verdict; it answers component states only. The MCP host SHALL NOT add HTTP authentication either, because the MCP server authenticates with its own OAuth flow. NGINX basic auth MUST NOT sit in front of Airflow: Airflow 3's UI sends its token in the `Authorization` header, which basic auth reads as missing credentials, so a browser would be challenged again on every call.

#### Scenario: No credentials

- **WHEN** `https://airflow.<domain>/api/v2/dags` is requested without a token
- **THEN** Airflow answers 401, and `https://airflow.<domain>/` serves Airflow's sign-in

#### Scenario: Health path without credentials

- **WHEN** `https://airflow.<domain>/api/v2/monitor/health` is requested without credentials
- **THEN** Airflow's health answer is served

#### Scenario: Browser sign-in

- **WHEN** a person opens `https://airflow.<domain>/` and signs in as `admin` with `AIRFLOW_ADMIN_PASSWORD`
- **THEN** the UI loads and stays signed in, with no further browser credential prompt

#### Scenario: Credential source

- **WHEN** the Airflow admin password is rotated in the secrets provider
- **THEN** the next stack start uses the new password with no project file changed

#### Scenario: MCP host without basic auth

- **WHEN** `https://mcp.<domain>/` is requested without credentials
- **THEN** the MCP server's own login page is served, not a 401 from NGINX

### Requirement: Secrets only through the provider interface

Runtime code in the stack SHALL obtain secrets, connection strings and the log target through the provider interface only. On the laptop the provider SHALL read `.env`; on a cloud it SHALL read the cloud's secrets store, and vecten SHALL write no secret to a file on the VM's disk: a file the stack needs with secret content, such as the Airflow passwords file or the NGINX basic-auth file, SHALL live in a memory-backed directory with owner-only access, rebuilt on every ship and at boot. A secret SHALL reach the disk in two places only: container environments, which the container runtime keeps in a root-only file on the VM's root volume (encrypted on AWS), and, on Scaleway, the VM's own API key, in a root-only file as the `shared/deploy` spec states. DAGs and the MCP server MUST NOT call a cloud SDK directly.

#### Scenario: Same DAG in both places

- **WHEN** a sync DAG runs on the laptop and on AWS
- **THEN** the DAG code is identical and only the provider behind it differs

#### Scenario: MCP server connections

- **WHEN** the MCP server starts on a VM
- **THEN** it obtains the `mcp_state` and warehouse connections through the provider and reads no file on the VM for them

### Requirement: One runtime write, scoped per VM

The only runtime write SHALL be the MCP server rotating the `impersonator` credential at `mcp/impersonator`, as the `shared/deploy` spec states. Each VM's identity SHALL read only its own secret scope and write only its own bucket prefix, apart from the setup host's generated credentials and its reads of each instance's master credential, which the `shared/deploy` spec names. On a VM, Airflow's task logs SHALL go to the `platform/logs` prefix of the environment's bucket (`s3://<bucket>/platform/logs` on both clouds, through Scaleway's S3-compatible endpoint there), inside the platform VM's prefix.

#### Scenario: DAG asks for an MCP secret

- **WHEN** a DAG on the platform VM asks the provider for `mcp/provisioner`
- **THEN** the cloud refuses the read

#### Scenario: Task logs on a VM

- **WHEN** a task runs on the platform VM of an AWS environment
- **THEN** its log is written under `s3://<bucket>/platform/logs` with the VM's own identity

### Requirement: Same images and Compose file, different profiles

The Compose file, the images, the DAGs, the dbt project and the `osi/` documents SHALL be identical on the laptop and on every VM. Only the profiles started, the source of secrets, the source of connection strings and the destination of logs SHALL differ, and all four SHALL be determined by `components`, `profile` and the provider.

#### Scenario: Shipped project

- **WHEN** `deploy` ships a project that ran locally with `up`
- **THEN** no file under `orchestration/`, `dbt/` or `osi/` differs between the laptop and the VMs

#### Scenario: Logs

- **WHEN** a task runs on the laptop
- **THEN** its logs are in a local Docker volume
- **AND** when the same task runs on a cloud VM its logs are in the environment's object storage bucket

### Requirement: Resource target

The `platform` profile SHALL run within the `small` tier's VM of 2 vCPU and 4 GB, its default; the `mcp` profile SHALL run within the VM class `size` selects. Under the `minimal` profile both Compose profiles SHALL run within one VM of at least 8 GB. Per-person connection pools and the Airflow pool SHALL be capped to the tier's connection budget.

#### Scenario: Default platform VM

- **WHEN** an environment is deployed with default tfvars
- **THEN** the platform VM has at least 2 vCPU and 4 GB of memory
- **AND** the stack starts and completes a sync DAG and a dbt build on it

#### Scenario: MCP VM follows size

- **WHEN** an environment is deployed under the `standard` profile with `size = "medium"`
- **THEN** the MCP VM is the medium entry and serves a compiled metric query within the documented latency with the medium user count connected

#### Scenario: Minimal

- **WHEN** an environment is deployed with `profile = "minimal"`
- **THEN** one VM of at least 8 GB runs both profiles and completes a sync DAG, a dbt build and a compiled metric query
