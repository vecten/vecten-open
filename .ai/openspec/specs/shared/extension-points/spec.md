# shared/extension-points Specification

## Purpose
How private and Enterprise extensions install into the open product without forks: one Python package per extension, one entry point group per layer it touches, and one command that wires every part.

## Requirements

### Requirement: Extensions are Python packages with per-layer entry points

An extension SHALL be a Python package that declares entry points in one or more of the groups `vecten.connectors`, `vecten.dbt_packages`, `vecten.dags`, `vecten.osi`, `vecten.mcp.tools`, `vecten.mcp.auth`, `vecten.mcp.provisioning`, `vecten.mcp.audit_sinks` and `vecten.skills`. Hooks for CLI commands and HTTP routes are not offered at v0.1 and SHALL be added with their first consumer.

#### Scenario: Multi-layer extension

- **WHEN** an entity resolution extension declares `vecten.dbt_packages`, `vecten.dags`, `vecten.osi` and `vecten.mcp.tools`
- **THEN** its dbt models are built, its DAG runs, its Ossie document is loaded and its tool is offered, each through its own group

#### Scenario: Provisioning hook

- **WHEN** an extension declares `vecten.mcp.provisioning`
- **THEN** the server calls it in place of its own provisioner for creating warehouse users and granting per-session membership, which is how a just-in-time or cloud IAM credential installs

#### Scenario: Audit sink

- **WHEN** an extension declares `vecten.mcp.audit_sinks`
- **THEN** every audit row is also handed to it after the local write, which is how signed export installs

### Requirement: extension add wires every declared part

`vecten extension add <package>` SHALL record the package under `extensions` in `vecten.toml`, add it to the project's requirements, and each declared group SHALL be honoured by the layer that owns it.

#### Scenario: Each group lands in its layer

- **WHEN** an extension declares every group
- **THEN** its connectors are found by `connector add`, its dbt packages added to `packages.yml` as `local:` paths, its DAGs mounted, its Ossie documents merged, its tools, identity backends, provisioning hooks and audit sinks registered, and its skills added to the plugin and `skills/`

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

### Requirement: Extensions are pip dependencies everywhere

An extension SHALL be resolved as an ordinary pip dependency, identically on a laptop, in CI, in the Airflow image and in the MCP image. Nothing from an extension SHALL be vendored into the project. The credential for a private package index SHALL come from the secrets store.

#### Scenario: Same version everywhere

- **WHEN** an extension is pinned in the project's requirements
- **THEN** the laptop, CI, the Airflow image and the MCP image install that same version

#### Scenario: Update

- **WHEN** the extension's version is bumped in the requirements
- **THEN** no file under the project changes other than the requirements and the lockfiles

### Requirement: Empty declarations are refused, unknown groups are reported

`vecten extension add` SHALL refuse a package that declares no entry point in any known group, and SHALL report, not refuse, a group it does not know, so a package built for a newer release still installs its known parts.

#### Scenario: Package without extension points

- **WHEN** `vecten extension add requests` runs
- **THEN** the command exits non-zero and says the package declares no Vecten extension point

#### Scenario: Unknown group

- **WHEN** a package declares `vecten.dashboards` beside `vecten.skills`
- **THEN** the skill is wired and the command reports `vecten.dashboards` as not known to this release

### Requirement: A stub extension proves every layer

The test suite SHALL carry a stub extension declaring all nine groups, and a test SHALL assert that each declared part lands in its layer.

#### Scenario: Stub test

- **WHEN** the stub extension is added to a fixture project and the test runs
- **THEN** a connector, a dbt package, a DAG, an Ossie document, a tool, an identity backend, a provisioning hook, an audit sink and a skill from the stub are each found in their layer
