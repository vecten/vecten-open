# platform/connectors Specification

## Purpose
How dlt sources are wired into a project: the two paths into `connector add`, how a custom source starts from a generic scaffold, what `connector add` renders and declares, how a source is run locally and inspected, and the deliberate absence of Vecten-authored connectors.

## Requirements

### Requirement: Add a dlt verified source by name

`connector add <source>` SHALL wire the dlt verified source of that name into the project, or the connector of that name provided by an installed extension through `vecten.connectors`. An unknown name SHALL be refused with an error that says the name is neither a dlt verified source nor an installed extension connector and points to the generic paths and the `connector` skill.

#### Scenario: Known verified source

- **WHEN** `connector add hubspot` is run
- **THEN** the HubSpot verified source is wired under `connectors/hubspot/`
- **AND** `hubspot` is declared in `vecten.toml`

#### Scenario: Extension connector

- **WHEN** an extension declaring an `attio` connector through `vecten.connectors` is listed in `vecten.toml` and `connector add attio` is run
- **THEN** the extension's connector is wired under `connectors/attio/`
- **AND** `attio` is declared in `vecten.toml` with the extension's `type` and `version`

#### Scenario: Unknown source name

- **WHEN** `connector add nosuchsource` is run
- **THEN** the command exits non-zero and nothing is rendered

### Requirement: Add a generic dlt source with a scaffold

`connector add <name> --from rest`, `--from sql` and `--from filesystem` SHALL render a scaffold for dlt's `rest_api`, `sql_database` and `filesystem` generic sources respectively, under `connectors/<name>/`, ready for an agent or user to fill in. A custom source SHALL start from one of these scaffolds, so that it receives the same rendered artefacts and declaration as every other source.

#### Scenario: REST scaffold

- **WHEN** `connector add billing --from rest` is run
- **THEN** `connectors/billing/` contains a `rest_api` source scaffold with placeholders for base URL, resources, pagination and incrementality

#### Scenario: Unknown generic kind

- **WHEN** `--from kafka` is given
- **THEN** the command exits non-zero and lists `rest`, `sql` and `filesystem`

### Requirement: What connector add renders

For every added source, `connector add` SHALL render `connectors/<source>/`, one sync DAG per object group in `orchestration/dags/`, a dbt `sources.yml` entry for the source's raw schema, and a connector test that replays recorded fixtures without network access.

#### Scenario: Rendered artefacts

- **WHEN** `connector add hubspot` completes
- **THEN** `connectors/hubspot/`, at least one sync DAG and a `sources.yml` entry for `raw_hubspot` exist
- **AND** the connector test is skipped with a message naming `connector run --record` until fixtures exist

### Requirement: Fixtures are recorded, never shipped

`connector run <source> --record` SHALL save the source's responses as fixtures under `connectors/<source>/fixtures/`, and the rendered connector test SHALL replay them offline. The product MUST NOT ship pre-recorded fixtures for any source.

#### Scenario: First recording

- **WHEN** `connector run hubspot --record` completes successfully
- **THEN** fixtures exist under `connectors/hubspot/fixtures/`
- **AND** the connector test passes with network access disabled

### Requirement: Connector declaration

Every connector SHALL be declared in `vecten.toml` with its name, a `source` that is one of `dlt` (a verified source), `rest`, `sql`, `filesystem` and `custom`, and, once answered, an `access.groups` list naming the groups that may read its raw schema. `connector run`, `raw schema` and `next` SHALL behave the same for every source kind. There is no connector type reserved for a paid tier; a connector from an extension is declared like any other.

#### Scenario: Declaration after add

- **WHEN** `connector add hubspot` completes
- **THEN** `vecten.toml` declares `hubspot` with `source = "dlt"`

#### Scenario: Access recorded on the declaration

- **WHEN** the access question for `hubspot` is answered with `sales` through `vecten answer`
- **THEN** the `hubspot` declaration carries `access.groups = ["sales"]`

#### Scenario: Wanted source becomes declared

- **WHEN** `hubspot` is in the `wanted` list and `connector add hubspot` completes
- **THEN** `hubspot` is removed from `wanted` and declared as a connector

#### Scenario: Scaffold replaced by the skill

- **WHEN** the `connector` skill replaces the body of a generic scaffold with its own source
- **THEN** the declaration's `source` becomes `custom`

#### Scenario: Connector from an extension

- **WHEN** `connector add` finds the source in an installed extension
- **THEN** it is declared with the kind the extension states and nothing marks it as paid

### Requirement: Sync DAG behaviour

Each rendered sync DAG SHALL run one task per object, with an independent checkpoint per object and retries at object level, so a failure in one object does not block or reset the others. It SHALL emit an Airflow asset per object group for the dbt DAG to schedule on, and SHALL post failures to the alert webhook when one is set.

#### Scenario: One object fails

- **WHEN** a sync DAG runs and one object's extraction fails
- **THEN** the other objects' tasks complete and keep their checkpoints
- **AND** the failed object is retried without re-extracting the succeeded ones

### Requirement: A new sync DAG syncs at once

A sync DAG SHALL run its first sync as soon as it is unpaused, not at the next schedule boundary. It SHALL create the connector's raw schema and dlt's staging schema before its object tasks start, so objects loading in parallel never race on creating them.

#### Scenario: Fresh deploy syncs at once

- **WHEN** a new `@daily` sync DAG is unpaused at 10:00 UTC
- **THEN** a run for the latest whole day starts without waiting for midnight

#### Scenario: First sync of a connector

- **WHEN** a connector's raw schema does not exist and its DAG runs two objects in parallel
- **THEN** the schema exists before either object task starts, and neither fails on creating it

### Requirement: Destination follows vecten.toml

The dlt destination for every source SHALL be derived from the warehouse declared in `vecten.toml`, with credentials obtained through the provider interface, so that no connector file names a warehouse or a credential.

#### Scenario: Postgres project

- **WHEN** `vecten.toml` declares `warehouse = "postgres"`
- **THEN** `connector run` loads into the Postgres warehouse the provider resolves

#### Scenario: Snowflake project

- **WHEN** `vecten.toml` declares `warehouse = "snowflake"`
- **THEN** the same connector code loads into Snowflake with no change to `connectors/`

### Requirement: Raw tables land in the source schema

Every sync SHALL write its tables into the schema `raw_<source>` and nowhere else.

#### Scenario: First sync

- **WHEN** `connector run hubspot` completes for the `companies` object
- **THEN** a `companies` table exists in `raw_hubspot`

### Requirement: Run a sync locally outside Airflow

`connector run <source> [--object X]` SHALL run the source's sync from the shell without Airflow, for all objects or for the one named, and SHALL report rows loaded per object. It SHALL exit non-zero when any object fails, and SHALL write a run record under `.vecten/` with the outcome per object for `next` to read.

#### Scenario: Single object

- **WHEN** `connector run hubspot --object companies` is run
- **THEN** only `companies` is extracted and loaded and its row count is reported
- **AND** the run record under `.vecten/` names `companies` as succeeded

#### Scenario: Source not added

- **WHEN** `connector run stripe` is run and `connectors/stripe/` does not exist
- **THEN** the command exits non-zero and suggests `connector add stripe`

### Requirement: Dump raw table schemas

`raw schema <source> --json` SHALL output the tables in `raw_<source>` with their columns and types as JSON, for use by the `model` skill.

#### Scenario: Schema after a run

- **WHEN** `raw schema hubspot --json` is run after a successful sync
- **THEN** the output lists every table in `raw_hubspot` with column names and types

#### Scenario: No raw tables yet

- **WHEN** `raw schema hubspot --json` is run before any sync
- **THEN** the output is an empty table list and the exit code is 0

### Requirement: No Vecten-authored connectors

The product MUST NOT ship any connector authored by Vecten. The only sources available are dlt verified sources, dlt generic sources, and sources written in the project by its users or agents.

#### Scenario: Connector inventory

- **WHEN** the installed package and a rendered project are inspected
- **THEN** neither contains connector code of its own, and the rendered `connectors/` holds only its package marker
- **AND** `connector add` offers only dlt's verified and generic sources
- **AND** no connector of type `vecten-managed` is in the package, although the declaration type is accepted
