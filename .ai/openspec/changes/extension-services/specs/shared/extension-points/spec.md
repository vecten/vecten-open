## ADDED Requirements

### Requirement: A service declaration states the process and its routes

An entry point in the group `vecten.services` SHALL declare one long-running service, named by the entry point's name, as a dictionary or a zero-argument factory returning one. Its known keys are `command` (a non-empty list of strings, run in exec form), `port` (the HTTP port it listens on, required when it has routes), `health` (`{"path": ...}` on `port`, or `{"command": [...]}`), `volume` (an absolute mount path, optional), `components` (the components whose project image holds its dependencies, `platform`, `mcp` or both, default `["platform"]`), `memory_mb` and `cpus` (default limits) and `routes`. Each route, named by its key, SHALL hold `path` (a path prefix starting with `/`, not under `/_vecten/`), a suggested `access` (`public` or `login`), suggested `groups` for `login`, and optionally `websocket` (boolean) and `max_body_mb` (1 to 100). Service and route names SHALL match `^[a-z][a-z0-9-]{0,30}$`, and two routes of one service SHALL NOT share a path. `vecten lock` SHALL record every declaration in `extensions.lock.json`, refuse a malformed known key naming the extension, the service and the key, and report an unknown key without refusing. `ship` and `up` SHALL read services from `extensions.lock.json` and `vecten.toml` only, never by importing an extension.

#### Scenario: Bot declared by an in-repo extension

- **WHEN** `extensions/acme-bot` declares the service `bot` with `command = ["python", "-m", "acme_bot.server"]`, `port = 8000`, `health = {"path": "/healthz"}` and a route `webhook` at `/api/messages`, and `vecten lock` runs
- **THEN** `extensions.lock.json` records the service and its route under `acme-bot`

#### Scenario: Route under the reserved prefix

- **WHEN** a service declares a route at `/_vecten/hooks`
- **THEN** `vecten lock` exits with the configuration error code naming the extension, the service and `path`

#### Scenario: CI ships without the extension

- **WHEN** the ship workflow runs `vecten ship --env prod` with only `vecten` installed, in a project binding a service of `acme-bot`
- **THEN** the bundle's Compose and NGINX files hold the service and its routes, rendered from `extensions.lock.json` and `vecten.toml`

#### Scenario: Stale record on the laptop

- **WHEN** an in-repo extension's service command changes in code and `vecten up` runs before `vecten lock`
- **THEN** `up` exits with the configuration error code naming the service and `vecten lock`, and no container starts

### Requirement: The project binds each service in vecten.toml

A declared service SHALL run only while `vecten.toml` binds it in `[services.<extension>.<service>]`, where `<extension>` is the normalised name of a listed extension, and a route SHALL be served only while `[services.<extension>.<service>.routes.<route>]` binds it. The binding SHALL hold `hostname` (a DNS label, or `@` for the apex of the environment's domain; required when any route is bound), `host` (one of the declaration's `components` that the project enables; default `platform` when listed and enabled, else required), `local` (default `true`), `memory_mb` and `cpus` (default the declaration's). A route's binding SHALL hold `access` (`public` or `login`), `groups` (a non-empty list, required for `login`, refused for `public`) and optionally `allow` (networks, validated and normalised as `[mcp] admin_allow` is). A binding naming an unlisted extension, an undeclared service or route, a reserved label (`airflow`, `mcp`, `www`, or one starting with `_`), a label bound twice, `@` where the domain equals the DNS zone, `login` in a project without the `mcp` component, or a `host` the declaration does not allow SHALL be refused when the configuration loads, naming the key. `vecten extension add` SHALL write a binding for each declared service and route from the declaration's suggestions, with `hostname` set to the service's name when it is not reserved or taken, and SHALL print each `public` route it bound.

#### Scenario: Binding written on add

- **WHEN** `vecten extension add ./extensions/acme-bot` runs for an extension whose service `bot` suggests a `public` route `webhook` and a `login` route `admin` for `default`
- **THEN** `vecten.toml` holds `[services.acme-bot.bot]` with `hostname = "bot"` and both routes with their suggested access and groups, and the output names `webhook` as public

#### Scenario: Unbound route

- **WHEN** an upgrade of `acme-bot` declares a new route `export` that `vecten.toml` does not bind
- **THEN** the route is not served, and `next` reports it as declared but not bound

#### Scenario: Login without the MCP

- **WHEN** a platform-only project binds a route with `access = "login"`
- **THEN** every command except `init` exits with the configuration error code naming the route and the `mcp` component

#### Scenario: Reserved label

- **WHEN** a binding sets `hostname = "mcp"`
- **THEN** loading the configuration fails naming the binding's `hostname`

## MODIFIED Requirements

### Requirement: Extensions are Python packages with per-layer entry points

An extension SHALL be a Python package that declares entry points in one or more of the groups `vecten.connectors`, `vecten.dbt_packages`, `vecten.dags`, `vecten.osi`, `vecten.mcp.tools`, `vecten.mcp.auth`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks`, `vecten.skills` and `vecten.services`, and MAY declare one `vecten.manifest` entry point stating the secrets and settings it needs. HTTP routes are declared as part of a service. Hooks for CLI commands are not offered and SHALL be added with their first consumer.

#### Scenario: Multi-layer extension

- **WHEN** an entity resolution extension declares `vecten.dbt_packages`, `vecten.dags`, `vecten.osi` and `vecten.mcp.tools`
- **THEN** its dbt models are built, its DAG runs, its Ossie document is loaded and its tool is offered, each through its own group

#### Scenario: Provisioning hook

- **WHEN** an extension declares `vecten.mcp.provisioning`
- **THEN** the server calls it in place of its own provisioner for creating warehouse users and granting per-session membership, which is how a just-in-time or cloud IAM credential installs

#### Scenario: Audit sink

- **WHEN** an extension declares `vecten.mcp.audit_sinks`
- **THEN** every audit row is also handed to it after the local write, which is how signed export installs

#### Scenario: Manifest beside layers

- **WHEN** an extension declares `vecten.dags` and `vecten.manifest`
- **THEN** its DAG runs and reads the settings and secrets its manifest declares

#### Scenario: Service beside a manifest

- **WHEN** an extension declares `vecten.services` and a manifest naming `ACME_BOT_TOKEN`
- **THEN** its bound service runs and reads `ACME_BOT_TOKEN` through the shared read API

### Requirement: A stub extension proves every layer

The test suite SHALL carry a stub extension declaring all eleven groups, with a manifest declaring a platform secret, an `MCP_` secret and a setting, with `platform`, `mcp` and `cli` extras that each name a different package, and with a service that has a `public` route and a `login` route. A test SHALL assert that each declared part lands in its layer.

#### Scenario: Stub test

- **WHEN** the stub extension is added to a fixture project and the test runs
- **THEN** a connector, a dbt package, a DAG, an Ossie document, a tool, an identity backend, a provisioning hook, an audit sink and a skill from the stub are each found in their layer
- **AND** its secrets and setting are in `extensions.lock.json`, and each extra's package is in its own component's lock only
- **AND** its service and both routes are in `extensions.lock.json`, bound in `vecten.toml`, and present in the generated Compose and NGINX files
