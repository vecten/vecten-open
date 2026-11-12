## ADDED Requirements

### Requirement: A service runs from its host component's project image

Each bound service SHALL run as one Compose service named `svc-<extension>-<service>`, from the project image of its `host` component (the Airflow project image for `platform`, the MCP project image for `mcp`), with the declared command, in the Compose profile of that component, with `restart: unless-stopped`. Its environment SHALL hold `VECTEN_ENV`, `VECTEN_COMPONENT=service`, `VECTEN_SERVICE` (`<extension>.<service>`), `VECTEN_PROJECT_ROOT`, `PORT` and, when a hostname is bound, `VECTEN_PUBLIC_URL` (`https://<hostname>`), and it SHALL mount `vecten.toml` and `extensions.lock.json` read-only, so the shared read API resolves its settings. It SHALL run with the memory and CPU limits of its binding. When `volume` is declared, the named volume `svc-<extension>-<service>-data` SHALL be mounted at that path, writable by the image's runtime user. No other service SHALL depend on it, and `nginx` SHALL NOT wait for it.

#### Scenario: Bot on the platform host

- **WHEN** `acme-bot`'s service `bot` is bound with `host = "platform"` and the platform host is shipped
- **THEN** `svc-acme-bot-bot` runs from the platform project image in the `platform` profile, with a 256 MB memory limit and its declared command

#### Scenario: Volume survives a ship

- **WHEN** the service writes a file under its declared volume and the next bundle is shipped
- **THEN** the file is still there after the service restarts

#### Scenario: Broken service

- **WHEN** a service exits at startup and restarts in a loop
- **THEN** `nginx`, Airflow and the MCP server keep serving, and the service's hostname answers 502

### Requirement: Services are isolated from the stack

Services SHALL join only the Compose network `vecten-services`, whose bridge name is fixed, and `nginx` SHALL join it beside the stack's default network; no other built-in service SHALL join it. A service SHALL publish no port on the host. Its environment SHALL hold no cloud key: no `SCW_*`, no `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY`, and no `AIRFLOW_CONN_*`. On a VM it SHALL NOT reach the cloud's instance metadata address, as the `shared/deploy` spec states. It SHALL receive, as environment variables, exactly the secrets its extension's manifest declares within the scopes its host may read, and the shared read API SHALL read a service's secrets from its environment.

#### Scenario: DAG calls a service directly

- **WHEN** a task in Airflow requests `http://svc-acme-bot-bot:8000/` with a forged `X-Vecten-User`
- **THEN** the name does not resolve on Airflow's network and nothing reaches the service

#### Scenario: Service asks for the instance role

- **WHEN** code in a service on an AWS VM requests `http://169.254.169.254/latest/api/token`
- **THEN** the connection fails and no credential is returned

#### Scenario: Undeclared secret

- **WHEN** service code calls `extension("acme-bot").secret("LOADER_PASSWORD")`
- **THEN** the call fails naming the extension and the secret, on the laptop and on a VM

### Requirement: Routes and their access modes

For each bound route whose hostname the environment serves, NGINX SHALL serve the route's path prefix on that hostname and forward it to the service's port, passing `Upgrade` and `Connection` for WebSockets. A route with `websocket = true` SHALL use a one-day read timeout with proxy buffering off; any other route a 300-second read timeout. The request body limit SHALL be the route's `max_body_mb`, 1 MB by default. A `public` route SHALL be forwarded for any client. A `login` route SHALL be forwarded only after the gate admits the request, as the `mcp/mcp-auth` spec states, and SHALL redirect an unauthenticated browser to the gate's sign-in on the same hostname and answer 403 to a person outside the route's groups. A non-empty `allow` SHALL answer 403 to every other address before any gate check, on deployed hosts only. On every service hostname, a path no bound route covers SHALL answer 404 and reach no service.

#### Scenario: Webhook

- **WHEN** a client posts to `https://bot.<domain>/api/messages` and the route `webhook` is `public`
- **THEN** the request reaches the service with the client's address in `X-Forwarded-For` and no `X-Vecten-*` header

#### Scenario: Person outside the groups

- **WHEN** a person signed in to the MCP and not in `finance` opens a `login` route bound to `["finance"]`
- **THEN** NGINX answers 403 and nothing is forwarded to the service

#### Scenario: Unbound path

- **WHEN** a client requests `https://bot.<domain>/internal/debug` and no route covers it
- **THEN** NGINX answers 404 and nothing is forwarded

#### Scenario: Streamlit WebSocket

- **WHEN** a signed-in person's browser opens the WebSocket of a `login` route with `websocket = true`
- **THEN** the upgrade is admitted by the gate and the connection stays open past five minutes of silence

### Requirement: The gate passes the person's identity, and nothing else may

On every location NGINX renders for a service hostname, it SHALL set `X-Vecten-User`, `X-Vecten-Principal`, `X-Vecten-Name`, `X-Vecten-Groups` and `X-Vecten-Admin` itself: on a `login` route to the values the gate returned for the request, and everywhere else to empty, so the header is not forwarded. No client-sent header of these names, in any letter case, SHALL reach a service. NGINX SHALL remove the gate's cookie from the `Cookie` header it forwards. `X-Vecten-Groups` SHALL list the person's current groups, comma-separated and sorted, and `X-Vecten-Admin` SHALL be `true` or `false`.

#### Scenario: Forged identity on a public route

- **WHEN** a client sends `X-Vecten-User: ada@acme.com` to a `public` route
- **THEN** the service receives no `X-Vecten-User` header

#### Scenario: Forged identity on a login route

- **WHEN** Bob, signed in, sends `x-vecten-user: ada@acme.com` to a `login` route
- **THEN** the service receives `X-Vecten-User: bob@acme.com` only

#### Scenario: Groups change while signed in

- **WHEN** an admin adds Bob to `finance` while Bob has a gate session
- **THEN** Bob's next request on a route bound to `["finance"]` is admitted and carries `finance` in `X-Vecten-Groups`

### Requirement: Service health is served openly and never blocks the front door

Each service SHALL have a Compose health check from its declaration. On a service hostname, NGINX SHALL serve `/_vecten/health` to every address, outside any `allow` list and the gate, admitting `GET` and `HEAD` only (any other method answers 403) and forwarding exactly the declared health path, whatever form of the URI it matched. A service declaring a health command and no health path SHALL have no such location.

#### Scenario: Health from a CI runner

- **WHEN** `ship` requests `https://portal.<domain>/_vecten/health` from an address outside the route's `allow` list
- **THEN** the service's health answer is returned

#### Scenario: Another request through the health path

- **WHEN** a client sends `POST /_vecten/health`, or a GET whose encoded path NGINX normalises to `/_vecten/health`
- **THEN** the POST is answered 403, and the GET reaches the service as a GET of its declared health path and nothing else

## MODIFIED Requirements

### Requirement: Compose services are grouped into profiles

The stack SHALL define the profiles `platform` (`airflow-api-server`, `airflow-scheduler`, `airflow-dag-processor`, `airflow-triggerer`, `log-cleaner`), `mcp` (`mcp-server`) and `local` (`postgres`), with `nginx` in every profile. The only other services SHALL be the services `vecten.toml` binds, each in the profile of its `host` component. `up` SHALL start the profiles that `components` names plus `local`. A VM SHALL run the profile of the component shipped to it and never the `local` profile.

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

NGINX SHALL route `airflow.<domain>` to the Airflow UI and `mcp.<domain>` to the MCP server, which serves the MCP endpoint, the OAuth pages and the admin panel. It SHALL route each bound service hostname that the host serves to that service's routes, its gate paths and its health path, and no service route SHALL be served on `airflow.<domain>` or `mcp.<domain>`. It SHALL refuse any other host or path. Both built-in hostnames SHALL be issued under every profile. On the laptop `up` SHALL print one URL per started component and per bound route of a started service, distinguished by port or by local hostnames.

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
