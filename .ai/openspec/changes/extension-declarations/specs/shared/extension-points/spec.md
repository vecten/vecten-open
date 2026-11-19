## ADDED Requirements

### Requirement: An extension declares what it needs in a manifest

An extension MAY declare one entry point in the group `vecten.manifest`, holding a dictionary or a zero-argument factory returning one. Its module SHALL import nothing beyond the standard library and `vecten_shared`. Its known keys are `secrets`, a list of secret names, and `settings`, a JSON Schema for the extension's settings. `vecten lock` SHALL load every listed extension's manifest, refuse a malformed known key with an error naming the extension and the key, and report, not refuse, an unknown top-level key, so that an extension built for a newer release still installs. A distribution declaring two `vecten.manifest` entry points SHALL be refused.

#### Scenario: Manifest with secrets and settings

- **WHEN** `acme-resolution` declares a manifest with `secrets = ["ACME_API_KEY", "MCP_ACME_TOKEN"]` and a `daily_cap` integer setting with default 1000, and `vecten lock` runs
- **THEN** `extensions.lock.json` records both names and the schema for `acme-resolution` at its locked version

#### Scenario: Key from a newer release

- **WHEN** a manifest also declares `permissions`
- **THEN** `vecten lock` succeeds and reports `permissions` as not supported by this release

#### Scenario: Malformed secrets

- **WHEN** a manifest declares `secrets = "ACME_API_KEY"`
- **THEN** `vecten lock` exits with the configuration error code naming `acme-resolution` and `secrets`, and no lock or record changes

### Requirement: A declared secret's name decides where it is delivered

A secret an extension's manifest declares, or that the top-level `secrets` list of `vecten.toml` declares for the project, SHALL follow the scope rule of the `shared/deploy` spec. An `MCP_*` name SHALL be delivered to the MCP server. Any other name SHALL be delivered to Airflow and to the CLI's own process. A name in the `deploy`, `admin` or `setup` scope, a name the stack manages, and any `AIRFLOW_*` name SHALL be refused. A name whose component the project does not enable SHALL be reported as not delivered, and not refused.

#### Scenario: One credential for a DAG and a tool

- **WHEN** an extension's DAG and its MCP tool both call the same API
- **THEN** its manifest declares `ACME_TOKEN` and `MCP_ACME_TOKEN`, `vecten secret set` stores the first in the `platform` scope and the second in the `mcp` scope, and each component reads only its own

#### Scenario: Reserved scope

- **WHEN** a manifest declares `DEPLOY_ACME_KEY`
- **THEN** `vecten lock` exits with the configuration error code, saying no host may read the `deploy` scope

#### Scenario: Platform-only project

- **WHEN** a project without the `mcp` component locks an extension declaring `MCP_ACME_TOKEN`
- **THEN** the lock succeeds and reports `MCP_ACME_TOKEN` as not delivered

### Requirement: Settings are namespaced in vecten.toml, with per-environment overrides

`vecten.toml` SHALL hold an extension's settings in `[settings.<name>]`, where `<name>` is the normalised distribution name of an extension listed in `extensions`, and the project's own settings in `[settings.project]`. Overrides for one environment SHALL sit in `[settings.<namespace>.env.<env>]`, where `<env>` is `local` for the laptop or a valid environment name. The effective value of a setting in an environment SHALL be the override for that environment if set, else the namespace's value, else the manifest's default. An override SHALL replace the whole value, with no deep merge. A namespace naming no listed extension, an invalid environment name, and a TOML date or time value SHALL be refused when `vecten.toml` is loaded. An extension whose normalised name is `project` SHALL be refused.

#### Scenario: Staging and the laptop dry-run, prod does not

- **WHEN** `[settings.acme-resolution]` sets `dry_run = false`, and `env.staging` and `env.local` set `dry_run = true`
- **THEN** the extension reads `true` on the laptop and in staging, and `false` in prod

#### Scenario: Default from the manifest

- **WHEN** the manifest gives `daily_cap` a default of 1000 and `vecten.toml` does not set it
- **THEN** the extension reads 1000 in every environment

#### Scenario: Unlisted namespace

- **WHEN** `vecten.toml` holds `[settings.acme-other]` and `acme-other` is not in `extensions`
- **THEN** every command except `init` exits with the configuration error code naming `settings.acme-other`

### Requirement: Settings are checked against the manifest before code runs

An extension's settings SHALL be valid against its manifest's schema, as recorded in `extensions.lock.json`. Vecten SHALL accept a schema of top-level `properties`, each typed `string`, `integer`, `number`, `boolean`, `array` or `object`, with optional `default`, `description`, `enum`, `minimum`, `maximum` and `items`, plus `required`. It SHALL treat additional properties as refused, and SHALL refuse a property named `env`. `vecten lock` SHALL check the effective values for `local` and for every environment named in an override. `up` SHALL check them for `local`, and `ship` and `ship --check` for the environment shipped. An unknown key, a value of the wrong type, or a required value without a default that is not set SHALL exit with the configuration error code, naming `settings.<namespace>.<key>` and the environment, before any container starts. Settings for an extension without a manifest `settings` schema SHALL be refused. `[settings.project]` has no schema and is checked for JSON types only.

#### Scenario: Typo in a key

- **WHEN** `[settings.acme-resolution]` sets `dialy_cap = 500`
- **THEN** `vecten up` exits with the configuration error code naming `settings.acme-resolution.dialy_cap` as unknown, and no container starts

#### Scenario: Wrong type in one environment

- **WHEN** `[settings.acme-resolution.env.prod]` sets `daily_cap = "500"`
- **THEN** `vecten ship --env prod` exits with the configuration error code naming the key and `prod`, before any host changes

### Requirement: One read API for settings and secrets

`vecten_shared.context` SHALL offer `extension(<name>)` and `project()` to DAG code, connector code and MCP tool code alike. Each SHALL return a handle whose `settings` is a read-only mapping of the effective values for `VECTEN_ENV`, whose `env` names that environment, and whose `secret(<NAME>)` returns a secret through the provider. `secret` SHALL refuse a name the handle's extension, or the project, did not declare, and a name outside the reading component's scope, on every provider and under every profile. The API SHALL read only `vecten.toml`, `extensions.lock.json` and the provider, so it imports neither runtime package nor the CLI. `vecten_platform.sync.secret` SHALL keep working for connector code.

#### Scenario: A DAG reads a setting and a secret

- **WHEN** a DAG from `acme-resolution` calls `extension("acme-resolution").settings["daily_cap"]` and `.secret("ACME_API_KEY")`
- **THEN** it gets the effective value for its environment and the secret's value, identically on the laptop and on a VM

#### Scenario: Undeclared secret

- **WHEN** extension code calls `extension("acme-resolution").secret("HUBSPOT_API_KEY")` and the manifest does not declare it
- **THEN** the call fails, naming the extension and the secret, on the laptop and on a VM alike

#### Scenario: Other component's secret under minimal

- **WHEN** a DAG on the one VM of a `minimal` environment calls `.secret("MCP_ACME_TOKEN")`
- **THEN** the call fails, saying `MCP_` secrets are delivered to the MCP server, although the VM's identity could read it

## MODIFIED Requirements

### Requirement: Extensions are Python packages with per-layer entry points

An extension SHALL be a Python package that declares entry points in one or more of the groups `vecten.connectors`, `vecten.dbt_packages`, `vecten.dags`, `vecten.osi`, `vecten.mcp.tools`, `vecten.mcp.auth`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks` and `vecten.skills`, and MAY declare one `vecten.manifest` entry point stating the secrets and settings it needs. Hooks for CLI commands and HTTP routes are not offered at v0.1 and SHALL be added with their first consumer.

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

### Requirement: extension add wires every declared part

`vecten extension add <package>` SHALL record the package under `extensions` in `vecten.toml`, pin it in the project's requirements, re-resolve the locks and the extension record as `vecten lock` does, and each declared group SHALL be honoured by the layer that owns it.

#### Scenario: Each group lands in its layer

- **WHEN** an extension declares every group
- **THEN** its connectors are found by `connector add`, its dbt packages added to `packages.yml` as `local:` paths, its DAGs mounted, its Ossie documents merged, its tools, identity backends, provisioning hooks and audit sinks registered, its skills added to the plugin and `skills/`, and its manifest's secrets and settings schema recorded in `extensions.lock.json`

#### Scenario: Add an extension

- **WHEN** `vecten extension add vecten-ext-entity-resolution` runs
- **THEN** `vecten.toml` lists the package under `extensions` and the requirements include it

#### Scenario: dbt package resolved

- **WHEN** `vecten dbt deps` runs in a project whose extension declares `vecten.dbt_packages`
- **THEN** `packages.yml` holds a `local:` entry pointing at the installed package's dbt directory
- **AND** `dbt-build-local` builds its models

#### Scenario: Skill rendered

- **WHEN** an extension declares `vecten.skills`
- **THEN** the skill appears under `skills/` in the project and in the plugin

#### Scenario: New secrets on an upgrade

- **WHEN** `extension add 'acme-resolution==1.3.0'` upgrades a version whose manifest adds `ACME_WEBHOOK_SECRET`
- **THEN** the command's output names the new secret with its `vecten secret set` command, and the diff of `extensions.lock.json` shows it

### Requirement: Extensions are pip dependencies everywhere

An extension SHALL be resolved as an ordinary pip dependency, at the same version on a laptop, in CI, in the Airflow image and in the MCP image. Nothing from an extension SHALL be vendored into the project. The extension's own dependencies SHALL be scoped per component through its standard extras: the extra `platform` is installed in the Airflow image, `mcp` in the MCP image and `cli` in the CLI's environment, each only where the wheel provides it, while its base dependencies are installed in all three. The credential for a private package index SHALL come from the secrets store.

#### Scenario: Same version everywhere

- **WHEN** an extension is pinned in the project's requirements
- **THEN** the laptop, CI, the Airflow image and the MCP image install that same version

#### Scenario: Heavy dependency for the DAG only

- **WHEN** an extension declares `lightgbm` in its `platform` extra and nothing in `mcp` or `cli`, and `vecten lock` runs
- **THEN** `requirements-airflow.lock` pins `lightgbm`, and neither `requirements-mcp.lock` nor `requirements.lock` holds it

#### Scenario: Extension without extras

- **WHEN** an extension declares no `platform`, `mcp` or `cli` extra
- **THEN** its dependencies are pinned in every enabled component's lock, as before

#### Scenario: Update

- **WHEN** the extension's version is bumped in the requirements
- **THEN** no file under the project changes other than the requirements, the lockfiles and `extensions.lock.json`

### Requirement: Empty declarations are refused, unknown groups are reported

`vecten extension add` SHALL refuse a package that declares no entry point in any known layer group, where `vecten.manifest` alone does not count as one, and SHALL report, not refuse, a group it does not know, so a package built for a newer release still installs its known parts.

#### Scenario: Package without extension points

- **WHEN** `vecten extension add requests` runs
- **THEN** the command exits non-zero and says the package declares no Vecten extension point

#### Scenario: Manifest only

- **WHEN** a package declares only `vecten.manifest`
- **THEN** the command exits non-zero and says the package declares no Vecten extension point

#### Scenario: Unknown group

- **WHEN** a package declares `vecten.dashboards` beside `vecten.skills`
- **THEN** the skill is wired and the command reports `vecten.dashboards` as not known to this release

### Requirement: A stub extension proves every layer

The test suite SHALL carry a stub extension declaring all ten groups, with a manifest declaring a platform secret, an `MCP_` secret and a setting, and with `platform`, `mcp` and `cli` extras that each name a different package. A test SHALL assert that each declared part lands in its layer.

#### Scenario: Stub test

- **WHEN** the stub extension is added to a fixture project and the test runs
- **THEN** a connector, a dbt package, a DAG, an Ossie document, a tool, an identity backend, a provisioning hook, an audit sink and a skill from the stub are each found in their layer
- **AND** its secrets and setting are in `extensions.lock.json`, and each extra's package is in its own component's lock only
