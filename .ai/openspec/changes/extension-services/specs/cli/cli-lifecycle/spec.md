## MODIFIED Requirements

### Requirement: up starts the local stack

`up` SHALL start the project's Docker Compose stack on the local machine with the profiles that `components` names plus the `local` profile, and every bound service whose binding does not set `local = false`. It SHALL wait until every started front door answers behind NGINX, SHALL print the Airflow URL when `platform` is present, the MCP URL when `mcp` is present and the URL of each bound route of a started service, at `https://<label>.localhost:8443`, and SHALL leave a record under `.vecten/` for `next`. With `platform` it SHALL also print the loopback Airflow API. It SHALL NOT wait for services to become healthy, and SHALL report each started service's health. `up` requires no cloud account or cloud credentials.

#### Scenario: Local stack on a laptop

- **WHEN** `up` is run in a rendered project with both components on a machine with Docker and no cloud credentials
- **THEN** the `platform`, `mcp` and `local` profiles are running in Docker
- **AND** the printed Airflow URL answers 401 without credentials and serves the Airflow UI with them
- **AND** the printed MCP URL serves the login page

#### Scenario: MCP-only project

- **WHEN** `up` is run in a project whose `components` is `mcp` only
- **THEN** no Airflow service is started and only the MCP URL is printed

#### Scenario: Airflow API for the Airflow MCP server

- **WHEN** `up --json` has started a project with the `platform` component
- **THEN** it prints `airflow_api` as `http://127.0.0.1:8482`, served without NGINX, and `airflow_user` as `admin`
- **AND** it names `AIRFLOW_ADMIN_PASSWORD` in `.env` as the password and never prints its value

#### Scenario: Portal on the laptop

- **WHEN** `up` runs in a project binding a portal service with `hostname = "portal"` and a `login` route at `/`
- **THEN** it prints `https://portal.localhost:8443/`, which redirects to the local MCP's login and, after sign-in, serves the portal

#### Scenario: Service skipped on the laptop

- **WHEN** a bot's binding sets `local = false`
- **THEN** `up` starts no container for it and prints no URL for its routes

#### Scenario: Docker not available

- **WHEN** `up` is run and Docker is not reachable
- **THEN** the command exits non-zero with an error that names Docker as the missing prerequisite

### Requirement: extension add

`vecten extension add <package>` SHALL record the package under `extensions` in `vecten.toml`, pin it in `requirements.txt`, write the locks and `extensions.lock.json` as `vecten lock` does, and wire every entry point group it declares among `vecten.connectors`, `vecten.dbt_packages`, `vecten.dags`, `vecten.osi`, `vecten.mcp.tools`, `vecten.mcp.auth`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks`, `vecten.skills`, `vecten.services` and `vecten.manifest`. A package declaring none of these groups, or `vecten.manifest` alone, SHALL be refused; an unknown group is reported, not refused. The command's output SHALL name each secret the extension declares that is not yet set locally, with its `vecten secret set` command. For `vecten.services` it SHALL write a binding for each service not yet bound, as the `shared/extension-points` spec states, leave existing bindings unchanged, and name each `public` route and each hostname that needs `vecten deploy`.

#### Scenario: Extension with several parts

- **WHEN** `vecten extension add vecten-ext-entity-resolution` runs and the package declares `vecten.dbt_packages`, `vecten.dags` and `vecten.osi`
- **THEN** `vecten.toml` lists the extension, the requirements include it, and `next` reports no unwired part

#### Scenario: Package without entry points

- **WHEN** `vecten extension add requests` runs
- **THEN** the command exits non-zero and lists the eleven groups an extension may declare

#### Scenario: Secrets to set

- **WHEN** `vecten extension add acme-resolution` adds an extension whose manifest declares `ACME_API_KEY`, and `.env` lacks it
- **THEN** the output names `vecten secret set ACME_API_KEY`, and `next` reports it for every environment until it is set there

#### Scenario: Upgrade keeps the project's binding

- **WHEN** `vecten extension add 'acme-bot==1.3.0'` upgrades an extension whose binding sets `access = "login"` on a route the new version suggests as `public`
- **THEN** the binding still says `login`, and the output names the difference
