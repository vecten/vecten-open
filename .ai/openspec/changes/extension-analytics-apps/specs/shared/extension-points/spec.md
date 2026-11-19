## ADDED Requirements

### Requirement: An app declaration states what an analytics app is and reads

An entry point in the group `vecten.apps` SHALL declare one analytics app, named by the entry point's name, as a dictionary or a zero-argument factory returning one, with `title`, `description`, `kind` (`container` or `snowflake`), `datasets` (a non-empty list of Ossie dataset names) and suggested `groups`. A `container` app SHALL give either `runtime = "streamlit"` with `entry`, a file path inside the installed package, or `command` and `port`; it MAY give `health`, `components`, `memory_mb` and `cpus` as a service does. A `snowflake` app SHALL give `entry`, its main file inside the package. `vecten lock` SHALL record each declaration in `extensions.lock.json` and refuse a malformed known key naming the extension, the app and the key. No product code SHALL depend on a particular BI tool other than the `streamlit` runtime shorthand.

#### Scenario: Streamlit app in an in-repo extension

- **WHEN** `extensions/acme-sales` declares the app `overview` with `kind = "container"`, `runtime = "streamlit"`, `entry = "acme_sales/app.py"` and `datasets = ["orders"]`, and `vecten lock` runs
- **THEN** `extensions.lock.json` records the app under `acme-sales`

#### Scenario: App without datasets

- **WHEN** an app declares `datasets = []`
- **THEN** `vecten lock` exits with the configuration error code naming the app and `datasets`

### Requirement: The project binds each app in vecten.toml

A declared app SHALL be published only while `vecten.toml` binds it in `[apps.<extension>.<app>]`, with `groups`, a non-empty list of MCP groups, and, for a `container` app, `hostname` (default the app's name) and `local` (default `true`). A bound `container` app SHALL run as the service `svc-<extension>-app-<app>` with one `login` route at `/` for the bound groups and `websocket = true`, following every rule of a bound service, with no `[services]` table. For `runtime = "streamlit"` the service's command SHALL be `python -m vecten_shared.launch streamlit <entry>`, which starts Streamlit on `$PORT`, headless, with usage statistics off and cross-site request protection on, and its health path SHALL be `/_stcore/health`. The app's credential names SHALL be `APP_<APP>_CLIENT_ID` and `APP_<APP>_CLIENT_SECRET` on the platform host and `MCP_APP_<APP>_CLIENT_ID` and `MCP_APP_<APP>_CLIENT_SECRET` on the MCP host, where `<APP>` is the app's name upper-cased with `-` as `_`; they SHALL be delivered to the app's service as declared secrets with no manifest entry, beside `VECTEN_MCP_URL` (`https://mcp.<domain>/mcp`). `vecten extension add` SHALL write a binding for each declared app from its suggestions and leave an existing binding unchanged. A binding for an undeclared app, or any app binding in a project without the `mcp` component, SHALL be refused when the configuration loads, because both who may open an app and what it reads are decided by the MCP.

#### Scenario: Container app shipped

- **WHEN** `[apps.acme-sales.overview]` binds `groups = ["sales"]` and `hostname = "sales"`, the record exists, and the platform host is shipped
- **THEN** `svc-acme-sales-app-overview` runs `python -m vecten_shared.launch streamlit acme_sales/app.py`, and `https://sales.<domain>/` serves it to signed-in members of `sales` only

#### Scenario: Streamlit sends no usage statistics

- **WHEN** the launcher starts a Streamlit app
- **THEN** the app's browser page makes no request to a Streamlit usage-statistics endpoint

#### Scenario: Snowflake app before warehouse resources exist

- **WHEN** a project binds a `snowflake` app and ships
- **THEN** the ship succeeds, and its output and the admin panel list the app as not published, naming the warehouse resources it needs

## MODIFIED Requirements

### Requirement: Extensions are Python packages with per-layer entry points

An extension SHALL be a Python package that declares entry points in one or more of the groups `vecten.connectors`, `vecten.dbt_packages`, `vecten.dags`, `vecten.osi`, `vecten.mcp.tools`, `vecten.mcp.auth`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks`, `vecten.skills`, `vecten.services` and `vecten.apps`, and MAY declare one `vecten.manifest` entry point stating the secrets and settings it needs. HTTP routes are declared as part of a service. Hooks for CLI commands are not offered and SHALL be added with their first consumer.

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

#### Scenario: Dashboard beside models

- **WHEN** an extension declares `vecten.dbt_packages` and `vecten.apps`
- **THEN** its models are built and its bound app is published and listed

### Requirement: Empty declarations are refused, unknown groups are reported

`vecten extension add` SHALL refuse a package that declares no entry point in any known layer group, where `vecten.manifest` alone does not count as one, and SHALL report, not refuse, a group it does not know, so a package built for a newer release still installs its known parts.

#### Scenario: Package without extension points

- **WHEN** `vecten extension add requests` runs
- **THEN** the command exits non-zero and says the package declares no Vecten extension point

#### Scenario: Manifest only

- **WHEN** a package declares only `vecten.manifest`
- **THEN** the command exits non-zero and says the package declares no Vecten extension point

#### Scenario: Unknown group

- **WHEN** a package declares `vecten.reports` beside `vecten.skills`
- **THEN** the skill is wired and the command reports `vecten.reports` as not known to this release

### Requirement: A stub extension proves every layer

The test suite SHALL carry a stub extension declaring all twelve groups, with a manifest declaring a platform secret, an `MCP_` secret and a setting, with `platform`, `mcp` and `cli` extras that each name a different package, with a service that has a `public` route and a `login` route, and with a `container` app and a `snowflake` app. A test SHALL assert that each declared part lands in its layer.

#### Scenario: Stub test

- **WHEN** the stub extension is added to a fixture project and the test runs
- **THEN** a connector, a dbt package, a DAG, an Ossie document, a tool, an identity backend, a provisioning hook, an audit sink and a skill from the stub are each found in their layer
- **AND** its secrets and setting are in `extensions.lock.json`, and each extra's package is in its own component's lock only
- **AND** its service and both routes are in `extensions.lock.json`, bound in `vecten.toml`, and present in the generated Compose and NGINX files
- **AND** both apps are in `extensions.lock.json` and bound, the container app's service and route are in the generated files, and the MCP's app list returns both
