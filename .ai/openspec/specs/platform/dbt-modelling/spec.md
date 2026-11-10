# platform/dbt-modelling Specification

## Purpose
The dbt project every rendered platform carries: which warehouse adapters it runs on, its layered layout, its dialect neutrality, the pass-through command, the credential-free local build harness and the generated Ossie datasets it leaves behind.

## Requirements

### Requirement: Adapter follows the warehouse choice

The rendered dbt project SHALL use dbt-core with the Postgres adapter when the warehouse is Postgres and the Snowflake adapter when the warehouse is Snowflake, with a profile that the CLI supplies so the user never writes one by hand.

#### Scenario: Postgres project

- **WHEN** a project is rendered with `warehouse=postgres`
- **THEN** `vecten dbt debug` connects through the Postgres adapter with the provider's connection string

#### Scenario: Snowflake project

- **WHEN** a project is rendered with `warehouse=snowflake`
- **THEN** `vecten dbt debug` connects through the Snowflake adapter

### Requirement: Layered layout

The dbt project SHALL be laid out as `staging/<source>_<entity>` models, one snapshot per staging model by default, then `core`, then `marts`, so data flows staging to snapshots to core to marts. A core entity MAY read several snapshots; every snapshot SHALL feed at least one core model.

#### Scenario: Layout after one modelled source

- **WHEN** a HubSpot `company` entity has been modelled
- **THEN** a `staging_hubspot_company` model, a `snapshot_hubspot_company` snapshot and a `company` core model exist
- **AND** the core model reads from the snapshot, not from raw

### Requirement: Dialect-neutral project

The rendered dbt project SHALL build on both Postgres and Snowflake from the same model code. Snowflake-only constructs MUST NOT appear in rendered models or macros.

#### Scenario: Same models on both adapters

- **WHEN** the fixture project is built on Postgres and on Snowflake
- **THEN** both builds succeed with identical model names and schemas

### Requirement: dbt pass-through

`vecten dbt <args>` SHALL invoke dbt with the given arguments against the project's profile and target, forwarding dbt's output and exit code unchanged. It SHALL NOT interpret any argument of its own, and it is the one command outside the CLI's `--json` and exit code contract.

#### Scenario: Forwarded command

- **WHEN** `vecten dbt run --select core` is run
- **THEN** dbt runs the `core` selection and `vecten` exits with dbt's exit code

### Requirement: dbt dependencies are resolved by vecten-platform

`vecten-platform` SHALL resolve dbt dependencies before every build: the `vecten_convention` package, which carries the convention tests and macros, taken from the installed `vecten-platform` wheel and checked against the version in the versions table, and a `local:` entry for every installed extension that declares `vecten.dbt_packages`, resolved from where the package is installed. `vecten dbt deps`, `dbt-build-local` and the dbt DAG SHALL all go through it, so extension and convention models build identically on a laptop, in CI and on the VM. `dbt/packages.yml` SHALL be generated before every run with `local:` paths, which are machine-specific, so it and the `dbt/package-lock.yml` that `dbt deps` writes SHALL be git-ignored.

#### Scenario: Extension dbt package

- **WHEN** an extension declaring `vecten.dbt_packages` is listed in `vecten.toml` and dependencies are resolved
- **THEN** `packages.yml` holds a `local:` entry pointing at the installed package's dbt directory
- **AND** the extension's models build with the project

#### Scenario: Convention package

- **WHEN** a fresh project resolves dependencies
- **THEN** `packages.yml` holds a `local:` entry for the `vecten_convention` package bundled in the installed wheel, whose version matches the versions table, and the convention tests run from it

### Requirement: Credential-free local build

`vecten dbt-build-local` SHALL build and test the whole project with no warehouse credentials: against a disposable Postgres container for Postgres projects, and against a Snowflake emulation on DuckDB for Snowflake projects. dbt SHALL build against an empty Ossie directory, because `osi/datasets.json` is the build's output and not its input, in a database named after `[warehouse] database` (`warehouse` when unset) so the generated sources match the build. After a green build it SHALL run the shared generator, rewrite `osi/datasets.json` from the manifest and catalog, and check the root `osi/` with `dbt parse`, failing and naming the document dbt cannot parse. The disposable environment SHALL be removed afterwards, and the result SHALL be written as a record under `.vecten/` for `next` to read.

#### Scenario: Postgres project on a laptop

- **WHEN** `dbt-build-local` is run in a Postgres project with Docker and no credentials
- **THEN** a temporary Postgres container is started, the project builds and tests, and the container is removed

#### Scenario: Snowflake project without an account

- **WHEN** `dbt-build-local` is run in a Snowflake project with no Snowflake credentials
- **THEN** the project builds and tests against the DuckDB-backed emulation

#### Scenario: Convention test failure

- **WHEN** `dbt-build-local` runs and a convention test fails
- **THEN** the command exits non-zero and the failing test and model are named in the output
- **AND** `osi/datasets.json` is not rewritten

#### Scenario: Renamed mart after its metric is edited

- **WHEN** a mart is renamed and the metric that used it is edited in `osi/metrics.json` first
- **THEN** `dbt-build-local` passes and `osi/datasets.json` names the new mart and not the old one

#### Scenario: Description change reaches the datasets file

- **WHEN** a mart column's description is changed in `schema.yml` and `dbt-build-local` passes
- **THEN** `osi/datasets.json` carries the new description on that field
- **AND** `osi/metrics.json` is unchanged

### Requirement: Generated datasets must be fresh in CI

CI SHALL build the project, regenerate `osi/datasets.json` and compare it with the committed file, failing the run when they differ, so the model the `mcp` component loads from git never lags the dbt project.

#### Scenario: Stale datasets committed

- **WHEN** a pull request changes a mart model and does not commit the regenerated `osi/datasets.json`
- **THEN** CI fails and names `osi/datasets.json` as stale

### Requirement: dbt runs under Airflow from the shipped bundle

The rendered project SHALL include an Airflow DAG that builds the dbt project from the shipped content bundle against the environment's warehouse, using the same profile mechanism as the CLI and resolving dbt dependencies through `vecten-platform` first. It SHALL be scheduled on the Airflow assets the sync DAGs emit, SHALL archive `run_results.json` and `sources.json` to the bucket per run, and after a green build SHALL write the bundle's commit as the environment's built commit.

#### Scenario: Scheduled build

- **WHEN** the dbt DAG runs on the deployed stack
- **THEN** the dbt project builds against the environment's warehouse, the run's success or failure is visible in Airflow, its results are archived to the bucket, and on success the built commit advances to the bundle's commit

#### Scenario: Freshness

- **WHEN** the dbt DAG runs source freshness on `_loaded_at` and a source is stale
- **THEN** `sources.json` in the bucket records it and `vecten issues` lists it resolved by `connector`
