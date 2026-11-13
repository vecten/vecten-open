## ADDED Requirements

### Requirement: An allow-list narrows who reaches the deployed Airflow host

`[airflow] allow` in `vecten.toml` SHALL list the IPv4 or IPv6 networks that may reach `airflow.<domain>` on a deployed host. Each entry SHALL be parsed as a network, a bare address meaning that one address, and normalised to CIDR notation, as `[mcp] admin_allow` is. An entry that is not a network, or that has host bits set, SHALL be refused, and so SHALL a non-empty list in a project without the `platform` component. When the list is non-empty, NGINX on the host serving Airflow SHALL answer every request from another address with 403, except requests for Airflow's health path `/api/v2/monitor/health`, which SHALL stay open to every address, because `ship` polls it from wherever it runs. That opening SHALL admit only `GET` and `HEAD`, answering any other method with 403, and SHALL forward exactly `/api/v2/monitor/health` to Airflow, whatever form of the URI NGINX matched, so a request NGINX normalises onto the path reaches no other Airflow route. When the list is empty, the default, the Airflow server SHALL be rendered exactly as without the setting. The laptop's stack SHALL ignore the list. The certificate challenge and the redirect on port 80, and the MCP host, SHALL be unaffected by it. Airflow's own login SHALL still apply to every address the list admits.

#### Scenario: Address outside the list

- **WHEN** `[airflow] allow = ["203.0.113.0/24"]` is shipped and a client at 198.51.100.7 requests `https://airflow.<domain>/`
- **THEN** NGINX answers 403 and nothing is proxied to Airflow

#### Scenario: Address inside the list

- **WHEN** a client at 203.0.113.9 requests `https://airflow.<domain>/`
- **THEN** Airflow's sign-in is served

#### Scenario: Ship health poll from a runner

- **WHEN** `ship` polls `https://airflow.<domain>/api/v2/monitor/health` from an address outside the list
- **THEN** Airflow's health answer is served, and the ship is judged on it

#### Scenario: Another request through the open health path

- **WHEN** a client outside the list sends `POST /api/v2/monitor/health`, or a GET whose encoded path NGINX normalises to `/api/v2/monitor/health`
- **THEN** the POST is answered 403, and the GET reaches Airflow as `GET /api/v2/monitor/health` and nothing else

#### Scenario: Invalid entry

- **WHEN** `vecten.toml` sets `[airflow] allow = ["10.0.0.1/8"]`
- **THEN** loading the configuration fails with a configuration error naming `airflow.allow`

#### Scenario: Issues from outside the list

- **WHEN** `vecten issues --env <env>` runs from an address outside the list
- **THEN** the Airflow source is reported as unreadable, with the 403 and a pointer to `[airflow] allow`

### Requirement: New DAGs start paused on the laptop, and on a VM when the environment opts in

The laptop's Airflow SHALL start every new DAG paused. On a VM, a new DAG SHALL start unpaused unless the environment's `deploy/envs/<env>.tfvars` sets `dags_paused_at_creation = true`. `ship` SHALL read the setting when it renders the host's Compose file. The setting SHALL be optional and SHALL be `true` or `false`; any other value SHALL be refused. Because Airflow applies the setting only when it first sees a DAG, a DAG that already exists SHALL keep its paused state when the setting changes.

#### Scenario: Default deployed environment

- **WHEN** an environment whose tfvars do not set `dags_paused_at_creation` is shipped
- **THEN** its Airflow services run with `AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION=False`

#### Scenario: Environment opts in

- **WHEN** `deploy/envs/staging.tfvars` sets `dags_paused_at_creation = true` and a new sync DAG is shipped to staging
- **THEN** the DAG appears paused in staging's Airflow, and no run starts until someone unpauses it
- **AND** an environment whose tfvars do not set it still starts the same DAG unpaused

#### Scenario: Laptop

- **WHEN** `up` starts the stack
- **THEN** every new DAG starts paused

#### Scenario: Invalid value

- **WHEN** the tfvars set `dags_paused_at_creation = "yes"`
- **THEN** `ship` refuses the environment with a configuration error naming the key

## MODIFIED Requirements

### Requirement: Same images and Compose file, different profiles

The Compose file, the images, the DAGs, the dbt project and the `osi/` documents SHALL be identical on the laptop and on every VM. Only the profiles started, the source of secrets, the source of connection strings, the destination of logs and whether new DAGs start paused SHALL differ. The first four SHALL be determined by `components`, `profile` and the provider, and the last by whether the stack runs on the laptop and by the environment's `dags_paused_at_creation`.

#### Scenario: Shipped project

- **WHEN** `deploy` ships a project that ran locally with `up`
- **THEN** no file under `orchestration/`, `dbt/` or `osi/` differs between the laptop and the VMs

#### Scenario: Logs

- **WHEN** a task runs on the laptop
- **THEN** its logs are in a local Docker volume
- **AND** when the same task runs on a cloud VM its logs are in the environment's object storage bucket
